"""Read-only assembly of one OA Package from current local state."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.models import (
    ArchivedFile,
    ClassificationDecision,
    OAItem,
    ParseArtifact,
)
from oa_knowledge.runtime_paths import resolve_cache_path

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
            parsed: list[tuple[str, str]] = []
            hashes: list[str] = []
            for file in files:
                artifact = self._artifact(session, file)
                if artifact is None:
                    continue
                try:
                    path = resolve_cache_path(self._settings, artifact.output_relpath)
                    body = path.read_text(encoding="utf-8", errors="replace").strip()
                except (OSError, ValueError):
                    continue
                if not body:
                    continue
                parsed.append((file.original_name, body))
                hashes.append(artifact.product_sha256 or artifact.source_sha256)
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
                parsed_attachments=tuple(parsed),
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
                parsed_text="\n".join(body for _, body in parsed),
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
