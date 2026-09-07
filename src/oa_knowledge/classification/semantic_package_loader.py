"""Read-only assembly of one OA Package from current local state."""

from __future__ import annotations

import hashlib
import json
import signal
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.archive import sha256_file
from oa_knowledge.classification.parse_cache import ParseCacheService, ParseRequest
from oa_knowledge.config import Settings
from oa_knowledge.db.models import (
    ArchivedFile,
    ClassificationDecision,
    OAItem,
    ParseArtifact,
)
from oa_knowledge.parsers.format_router import detect_format, parser_attempts
from oa_knowledge.parsers.router import resolve_parser_version
from oa_knowledge.runtime_paths import resolve_cache_path, resolve_original_path

from .agnes_eligibility import (
    AgnesEligibility,
    AgnesEligibilityInput,
    determine_agnes_eligibility,
)
from .semantic_classifier import SemanticPackage


class DatabaseSemanticPackageLoader:
    """Load only durable parse products; this class never reads originals."""

    def __init__(self, session_factory: Callable[[], Session], settings: Settings) -> None:
        self._sessions = session_factory
        self._settings = settings
        self._parse_cache = ParseCacheService(session_factory, settings)

    def load_file_text(self, file_id: int) -> tuple[str, str] | None:
        """Read evidence for this verified file, never a cached classification."""
        with self._sessions() as session:
            file = session.get(ArchivedFile, file_id)
            if file is None or file.download_status != "verified" or not file.sha256:
                return None
            artifact = self._artifact(session, file)
            if artifact is not None and artifact.source_sha256 == file.sha256:
                try:
                    product = resolve_cache_path(self._settings, artifact.output_relpath)
                    if artifact.product_sha256 and sha256_file(product) == artifact.product_sha256:
                        cached = self._read_artifact(artifact)
                        if cached is not None:
                            return cached[0], file.sha256
                except (OSError, ValueError):
                    pass
            return self._parse_when_needed(file)

    def __call__(self, oa_item_key: str) -> tuple[SemanticPackage, AgnesEligibility]:
        with self._sessions() as session:
            item = session.scalar(select(OAItem).where(OAItem.oa_item_key == oa_item_key))
            decision = session.scalar(select(ClassificationDecision).where(
                ClassificationDecision.oa_item_key == oa_item_key,
                ClassificationDecision.is_current.is_(True),
            ))
            if item is None or decision is None:
                raise ValueError("semantic package has no current OA item/decision")
            files = list(session.scalars(select(ArchivedFile).where(
                ArchivedFile.oa_item_id == item.id,
                ArchivedFile.download_status == "verified",
            ).order_by(ArchivedFile.id)))
            parsed_attachments: list[tuple[str, str]] = []
            hashes: list[str] = []
            for file in files:
                loaded = self.load_file_text(file.id)
                if loaded is None:
                    continue
                body, _source_sha = loaded
                product_sha = hashlib.sha256(body.encode('utf-8')).hexdigest()
                parsed_attachments.append((file.original_name, body))
                hashes.append(product_sha)
            current = {
                "classification_status": decision.classification_status,
                "content_origin": decision.content_origin,
                "flow_type": decision.flow_type,
                "business_category": decision.business_category,
                "canonical_issuer": decision.canonical_issuer,
                "confidence": decision.classification_confidence,
                "source": decision.decision_source,
            }
            package = SemanticPackage(
                oa_item_key=oa_item_key,
                title=item.title,
                document_number=decision.document_number or item.document_number,
                attachment_names=tuple(file.original_name for file in files),
                parsed_attachments=tuple(parsed_attachments),
                parse_artifact_hashes=tuple(hashes),
                current_classification=current,
            )
            eligibility = determine_agnes_eligibility(AgnesEligibilityInput(
                title=item.title,
                content_origin=decision.content_origin,
                flow_type=decision.flow_type,
                document_number=package.document_number,
                canonical_issuer=decision.canonical_issuer,
                workflow=None,
                attachment_names=package.attachment_names,
                parsed_text="\n".join(body for _, body in parsed_attachments),
            ))
            return package, eligibility

    @staticmethod
    def _artifact(session: Session, file: ArchivedFile) -> ParseArtifact | None:
        if file.content_object_id is None:
            return None
        return session.scalar(select(ParseArtifact).where(
            ParseArtifact.content_object_id == file.content_object_id,
            ParseArtifact.lifecycle_status == "valid",
        ).order_by(ParseArtifact.created_at.desc(), ParseArtifact.id.desc()))

    def _read_artifact(self, artifact: ParseArtifact | None) -> tuple[str, str] | None:
        if artifact is None:
            return None
        try:
            path = resolve_cache_path(self._settings, artifact.output_relpath)
            body = path.read_text(encoding="utf-8", errors="replace").strip()
        except (OSError, ValueError):
            return None
        if not body or artifact.quality_score is not None and artifact.quality_score < 0.5:
            return None
        return body, artifact.product_sha256 or artifact.source_sha256

    def _parse_when_needed(self, file: ArchivedFile) -> tuple[str, str] | None:
        """Use the shared FormatRouter only after usable local text is absent."""
        if (
            file.download_status != "verified"
            or not file.local_relpath
            or not file.sha256
        ):
            return None
        try:
            source = resolve_original_path(self._settings, file.local_relpath)
        except ValueError:
            return None
        if not source.is_file() or (
            file.size_bytes is not None and source.stat().st_size != file.size_bytes
        ) or sha256_file(source) != file.sha256:
            return None
        decision = detect_format(source)
        if decision.is_direct_text:
            try:
                body = source.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                return None
            return (body, file.sha256) if body else None
        if decision.status_code != "parseable":
            return None
        for parser_name in parser_attempts(
            decision, mineru_enabled=self._settings.mineru.enabled
        )[:2]:
            parser_version = resolve_parser_version(parser_name, self._settings)
            request = ParseRequest(
                file_id=file.id,
                content_sha256=file.sha256,
                parser_name=parser_name,
                parser_version=parser_version,
                parse_profile_version="semantic-v2",
                parse_config_sha256=self._parse_config_sha(parser_name, decision.actual_file_type),
                metadata_unresolved=True,
                purpose="classification",
            )
            try:
                result = parse_with_semantic_timeout(
                    parser_name,
                    lambda request=request: self._parse_cache.get_or_parse(request),
                )
            except TimeoutError:
                continue
            if result.status != "parsed" or not result.output_relpath:
                continue
            try:
                body = resolve_cache_path(self._settings, result.output_relpath).read_text(
                    encoding="utf-8", errors="replace"
                ).strip()
            except (OSError, ValueError):
                continue
            if body and (result.quality_score is None or result.quality_score >= 0.5):
                return body, file.sha256
        return None

    def _parse_config_sha(self, parser_name: str, actual_file_type: str) -> str:
        payload = {
            "purpose": "semantic-v2",
            "engine": parser_name,
            "actual_file_type": actual_file_type,
            "parser": self._settings.parser.model_dump(mode="json"),
            "mineru": self._settings.mineru.model_dump(mode="json"),
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def parse_with_semantic_timeout(parser_name: str, operation: Callable[[], object]) -> object:
    """Bound one MinerU semantic parse so a single attachment cannot stall a run."""
    if parser_name != "mineru":
        return operation()
    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(
        signal.SIGALRM,
        lambda *_: (_ for _ in ()).throw(TimeoutError("mineru_semantic_timeout")),
    )
    signal.setitimer(signal.ITIMER_REAL, 90)
    try:
        return operation()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
