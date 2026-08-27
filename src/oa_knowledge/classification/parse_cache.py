"""On-demand, version-aware parse artifact cache for OA classification.

This adapter deliberately does not decide an OA classification.  Its only job
is to produce (or reuse) a local parse artifact after the caller has decided
that metadata is insufficient.  Parsed output is derived data below the local
runtime cache, never below the immutable originals tree.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from oa_knowledge.archive import atomic_write_bytes, sha256_file
from oa_knowledge.config import Settings
from oa_knowledge.db.models import (
    ArchivedFile,
    ContentObject,
    ParseArtifact,
    ParseJob,
    ReviewEntry,
)
from oa_knowledge.parsers.router import parse_file
from oa_knowledge.runtime_paths import (
    ensure_owned_directory,
    resolve_cache_path,
    resolve_original_path,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]+$")
_PARSEABLE_INTEGRITY = frozenset({"ok"})


class _ParserIdentityMismatch(RuntimeError):
    """The selected parser did not produce the requested cache identity."""


@dataclass(frozen=True, slots=True)
class ParseRequest:
    """One explicitly selected attachment parse request.

    ``metadata_unresolved`` is intentionally required in every request.  A
    metadata-resolved OA returns ``not_required`` before opening the original
    or selecting a parser.
    """

    file_id: int
    content_sha256: str | None
    parser_name: str
    parser_version: str
    parse_profile_version: str
    parse_config_sha256: str
    metadata_unresolved: bool
    content_integrity_status: str = "ok"
    depth_limit_reached: bool = False
    container_key: str | None = None


@dataclass(frozen=True, slots=True)
class ParseArtifactRef:
    artifact_id: int | None
    output_relpath: str | None
    consulted: bool
    status: str
    error_code: str | None = None


class ParseCacheService:
    """Create/reuse cache artifacts using the immutable ParseArtifact identity."""

    def __init__(
        self, session_factory: sessionmaker[Session], settings: Settings
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings

    def get_or_parse(self, request: ParseRequest) -> ParseArtifactRef:
        """Return the exact versioned parse artifact required by one unresolved item.

        The database unique key is deliberately the final concurrency arbiter.
        If two workers parse the same identity, the losing worker rolls back
        its insert and returns the durable winner.
        """
        if not request.metadata_unresolved:
            return ParseArtifactRef(None, None, consulted=False, status="not_required")
        invalid = self._validate_request(request)
        if invalid is not None:
            return ParseArtifactRef(
                None,
                None,
                consulted=False,
                status="integrity_blocked",
                error_code=invalid,
            )

        with self._session_factory() as session:
            file = session.get(ArchivedFile, request.file_id)
            if file is None:
                return ParseArtifactRef(
                    None,
                    None,
                    consulted=False,
                    status="integrity_blocked",
                    error_code="file_missing",
                )
            integrity_error = self._file_integrity_error(file, request)
            if integrity_error is not None:
                if integrity_error == "depth_limit_reached":
                    self._enqueue_depth_review(session, file, request.container_key)
                session.commit()
                return ParseArtifactRef(
                    None,
                    None,
                    consulted=False,
                    status="integrity_blocked",
                    error_code=integrity_error,
                )

            source = self._source_path(file)
            if source is None:
                return ParseArtifactRef(
                    None,
                    None,
                    consulted=False,
                    status="integrity_blocked",
                    error_code="file_missing",
                )
            if file.size_bytes is not None and source.stat().st_size != file.size_bytes:
                return ParseArtifactRef(
                    None,
                    None,
                    consulted=False,
                    status="integrity_blocked",
                    error_code="size_mismatch",
                )
            if sha256_file(source) != request.content_sha256:
                return ParseArtifactRef(
                    None,
                    None,
                    consulted=False,
                    status="integrity_blocked",
                    error_code="sha256_mismatch",
                )

            content = self._content_object(
                session, request.content_sha256, file, source
            )
            file.content_object_id = content.id
            cached = self._existing_artifact(session, content.id, request)
            if cached is not None:
                session.commit()
                return self._ref(cached)

            job = ParseJob(
                file_id=file.id,
                engine=request.parser_name,
                engine_version=request.parser_version,
                config_hash=request.parse_config_sha256,
                status="running",
                attempts=1,
            )
            session.add(job)
            session.flush()
            job_id = job.id
            session.commit()

        return self._parse_and_persist(request, source, content.id, job_id)

    def _parse_and_persist(
        self,
        request: ParseRequest,
        source: Path,
        content_object_id: int,
        job_id: int,
    ) -> ParseArtifactRef:
        root = ensure_owned_directory(
            self._settings.parse_work_root / "classification-parse"
        )
        staging = Path(tempfile.mkdtemp(prefix=".classification-parse-", dir=root))
        try:
            result = parse_file(
                source,
                self._settings,
                engine=request.parser_name,
                output_dir=staging,
                profile_version=request.parse_profile_version,
            )
            if not self._matches_requested_identity(result, request):
                raise _ParserIdentityMismatch("parser_identity_mismatch")
            if not result.output_path.is_file():
                raise RuntimeError("parse_output_missing")
            output_relpath = self._output_relpath(request)
            destination = atomic_write_bytes(
                result.output_path.read_bytes(),
                self._settings.cache_root,
                output_relpath,
            )
            product_sha = sha256_file(destination)
        except _ParserIdentityMismatch:
            self._mark_job_failed(job_id, error_code="parser_identity_mismatch")
            return ParseArtifactRef(
                None,
                None,
                consulted=True,
                status="parse_failed",
                error_code="parser_identity_mismatch",
            )
        except (OSError, RuntimeError, ValueError):
            self._mark_job_failed(job_id)
            return ParseArtifactRef(
                None,
                None,
                consulted=True,
                status="parse_failed",
                error_code="parse_failed",
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        try:
            with self._session_factory() as session:
                cached = self._existing_artifact(session, content_object_id, request)
                if cached is not None:
                    job = session.get(ParseJob, job_id)
                    if job is not None:
                        job.status = "duplicate"
                    session.commit()
                    return self._ref(cached)

                artifact = ParseArtifact(
                    parse_job_id=job_id,
                    content_object_id=content_object_id,
                    engine=request.parser_name,
                    engine_version=request.parser_version,
                    profile_version=request.parse_profile_version,
                    output_relpath=output_relpath,
                    source_sha256=request.content_sha256,
                    product_sha256=product_sha,
                    config_hash=request.parse_config_sha256,
                    page_map_json="{}",
                    quality_score=result.quality_score,
                    quality_status="low" if result.quality_score < 0.5 else "ok",
                    lifecycle_status="valid",
                )
                session.add(artifact)
                job = session.get(ParseJob, job_id)
                if job is not None:
                    job.status = "completed"
                    job.quality_score = result.quality_score
                    job.output_relpath = output_relpath
                session.commit()
                return self._ref(artifact)
        except IntegrityError:
            # A concurrent worker won the unique identity race. The product is
            # deterministic derived data. Reuse the durable row and refresh
            # its atomically-written product rather than manufacturing a
            # second artifact. This also heals a missing derivative whose row
            # still occupies the immutable cache identity.
            with self._session_factory() as session:
                artifact = self._identity_artifact(session, content_object_id, request)
                if artifact is None:
                    raise
                artifact.output_relpath = output_relpath
                artifact.product_sha256 = product_sha
                artifact.quality_score = result.quality_score
                artifact.quality_status = (
                    "low" if result.quality_score < 0.5 else "ok"
                )
                artifact.lifecycle_status = "valid"
                job = session.get(ParseJob, job_id)
                if job is not None:
                    job.status = "duplicate"
                session.commit()
                return self._ref(artifact)

    def _validate_request(self, request: ParseRequest) -> str | None:
        if request.content_sha256 is None or not _SHA256.fullmatch(
            request.content_sha256
        ):
            return "sha256_mismatch"
        if not _SHA256.fullmatch(request.parse_config_sha256):
            return "invalid_parse_config"
        if not all(
            _IDENTIFIER.fullmatch(value)
            for value in (
                request.parser_name,
                request.parser_version,
                request.parse_profile_version,
            )
        ):
            return "invalid_parser_identity"
        return None

    def _file_integrity_error(
        self, file: ArchivedFile, request: ParseRequest
    ) -> str | None:
        if request.depth_limit_reached:
            return "depth_limit_reached"
        if request.content_integrity_status not in _PARSEABLE_INTEGRITY:
            return "content_not_verified"
        if file.download_status != "verified":
            return "content_not_verified"
        if file.sha256 != request.content_sha256:
            return "sha256_mismatch"
        return None

    def _source_path(self, file: ArchivedFile) -> Path | None:
        if not file.local_relpath:
            return None
        try:
            source = resolve_original_path(self._settings, file.local_relpath)
        except ValueError:
            return None
        return source if source.is_file() else None

    @staticmethod
    def _content_object(
        session: Session, sha256: str, file: ArchivedFile, source: Path
    ) -> ContentObject:
        session.execute(
            sqlite_insert(ContentObject)
            .values(
                sha256=sha256,
                size_bytes=file.size_bytes
                if file.size_bytes is not None
                else source.stat().st_size,
                detected_type=source.suffix.lstrip(".") or "unknown",
            )
            .on_conflict_do_nothing(index_elements=("sha256",))
        )
        content = session.scalar(
            select(ContentObject).where(ContentObject.sha256 == sha256)
        )
        if content is None:
            raise RuntimeError("content_object_unavailable")
        return content

    @staticmethod
    def _matches_requested_identity(result: object, request: ParseRequest) -> bool:
        return (
            getattr(result, "engine", None) == request.parser_name
            and getattr(result, "engine_version", None) == request.parser_version
            and getattr(result, "profile_version", None) == request.parse_profile_version
        )

    def _existing_artifact(
        self, session: Session, content_object_id: int, request: ParseRequest
    ) -> ParseArtifact | None:
        artifact = self._identity_artifact(session, content_object_id, request)
        if artifact is None or artifact.lifecycle_status != "valid":
            return None
        try:
            product = resolve_cache_path(self._settings, artifact.output_relpath)
        except ValueError:
            return None
        if (
            not product.is_file()
            or artifact.product_sha256
            and sha256_file(product) != artifact.product_sha256
        ):
            return None
        return artifact

    @staticmethod
    def _identity_artifact(
        session: Session, content_object_id: int, request: ParseRequest
    ) -> ParseArtifact | None:
        return session.scalar(
            select(ParseArtifact).where(
                ParseArtifact.content_object_id == content_object_id,
                ParseArtifact.engine == request.parser_name,
                ParseArtifact.engine_version == request.parser_version,
                ParseArtifact.profile_version == request.parse_profile_version,
                ParseArtifact.config_hash == request.parse_config_sha256,
            )
        )

    @staticmethod
    def _enqueue_depth_review(
        session: Session, file: ArchivedFile, container_key: str | None
    ) -> None:
        existing = session.scalar(
            select(ReviewEntry.id).where(
                ReviewEntry.kind == "depth_limit_reached",
                ReviewEntry.file_id == file.id,
                ReviewEntry.status == "pending",
            )
        )
        if existing is None:
            session.add(
                ReviewEntry(
                    kind="depth_limit_reached",
                    item_id=file.oa_item_id,
                    file_id=file.id,
                    container_key=container_key or file.source_container_key,
                    depth=10,
                    details_json=json.dumps(
                        {"source": "classification_parse_cache"}, sort_keys=True
                    ),
                )
            )

    @staticmethod
    def _ref(artifact: ParseArtifact) -> ParseArtifactRef:
        return ParseArtifactRef(
            artifact_id=artifact.id,
            output_relpath=artifact.output_relpath,
            consulted=True,
            status="parsed",
        )

    @staticmethod
    def _output_relpath(request: ParseRequest) -> str:
        identity = json.dumps(
            {
                "content_sha256": request.content_sha256,
                "parser_name": request.parser_name,
                "parser_version": request.parser_version,
                "parse_profile_version": request.parse_profile_version,
                "parse_config_sha256": request.parse_config_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        artifact_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return (
            f"work/classification-parse/{request.content_sha256[:2]}/{artifact_key}.md"
        )

    def _mark_job_failed(self, job_id: int, *, error_code: str = "parse_failed") -> None:
        with self._session_factory() as session:
            job = session.get(ParseJob, job_id)
            if job is not None:
                job.status = "failed"
                job.error_code = error_code
                session.commit()
