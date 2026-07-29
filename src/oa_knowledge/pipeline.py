"""Parse job pipeline — enqueue, run, and manage document parsing jobs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from oa_knowledge.archive.integrity import sha256_file
from oa_knowledge.archive.naming import safe_filename
from oa_knowledge.config import Settings
from oa_knowledge.constants import PipelineStatus
from oa_knowledge.db.models import (
    ArchivedFile,
    ContentObject,
    KnowledgeDocument,
    OAItem,
    ParseArtifact,
    ParseJob,
)
from oa_knowledge.parsers.markitdown_parser import parse_with_markitdown
from oa_knowledge.parsers.mineru_parser import parse_with_mineru, mineru_available
from oa_knowledge.parsers.eligibility import evaluate_eligibility
from oa_knowledge.parsers.quality import assess_quality
from oa_knowledge.parsers.router import ParseResult, preflight


class ParsePipeline:
    """Manages the lifecycle of parse jobs: enqueue, run, and batch processing."""

    def __init__(self, settings: Settings, engine=None) -> None:
        self.settings = settings
        self._engine = engine

    @property
    def engine(self):
        if self._engine is None:
            from oa_knowledge.db.engine import create_db_engine
            self._engine = create_db_engine(self.settings.database_path)
        return self._engine

    def _get_output_base(self) -> Path:
        """Return the parse output directory under data_root."""
        parse_dir = self.settings.data_root / "parse"
        parse_dir.mkdir(parents=True, exist_ok=True)
        return parse_dir

    def enqueue(
        self,
        file_id: int,
        engine: str | None = None,
        session: Session | None = None,
    ) -> int | None:
        """Create a parse job for a file, idempotent by (file_id, sha256, engine).

        Returns the job_id if created, or existing job_id if already queued/completed.
        Returns None if the file doesn't exist or can't be parsed.
        """
        if session is None:
            return self._enqueue_internal(file_id, engine, create_session=True)

        return self._enqueue_internal(file_id, engine, session=session, create_session=False)

    def _enqueue_internal(
        self,
        file_id: int,
        engine: str | None,
        *,
        session: Session | None = None,
        create_session: bool = False,
    ) -> int | None:
        owner_session: Session | None = None
        if create_session:
            owner_session = Session(self.engine)
            session = owner_session

        try:
            file_rec = session.get(ArchivedFile, file_id)
            if file_rec is None or file_rec.download_status != "verified":
                return None

            if file_rec.local_relpath is None:
                return None

            relative = safe_filename(Path(file_rec.local_relpath).name)
            file_path = self.settings.data_root / file_rec.local_relpath
            if not file_path.is_file():
                return None

            eligibility = evaluate_eligibility(file_path)
            if not eligibility.eligible:
                return None

            # Compute idempotency key from file_id + sha256 + engine
            file_sha = file_rec.sha256 or sha256_file(file_path)
            target_engine = engine or self.settings.parser.default_engine

            content = session.execute(
                select(ContentObject).where(ContentObject.sha256 == file_sha)
            ).scalar_one_or_none()
            if content is None:
                content = ContentObject(
                    sha256=file_sha,
                    size_bytes=file_rec.size_bytes if file_rec.size_bytes is not None else file_path.stat().st_size,
                    detected_type=file_path.suffix.lower().lstrip(".") or "unknown",
                )
                session.add(content)
                session.flush()
            file_rec.content_object_id = content.id

            # Check for existing job with same idempotency key
            existing = session.execute(
                select(ParseJob).where(
                    ParseJob.file_id == file_id,
                    ParseJob.engine == target_engine,
                )
            ).scalar_one_or_none()

            if existing:
                if existing.status in ("completed",):
                    return existing.id
                # Return existing queued/running job
                return existing.id

            # Create parse job
            job = ParseJob(
                file_id=file_id,
                engine=target_engine,
                engine_version=self._engine_version(target_engine),
                config_hash="",  # filled in at completion
                status="queued",
            )
            session.add(job)
            session.flush()

            # Update parent OA item pipeline status
            item = session.execute(
                select(OAItem).join(ArchivedFile).where(ArchivedFile.id == file_id)
            ).scalar_one_or_none()
            if item and item.pipeline_status in (
                PipelineStatus.RAW_SAVED,
                PipelineStatus.FILES_VERIFIED,
            ):
                item.pipeline_status = PipelineStatus.PARSE_QUEUED

            session.commit()
            return job.id
        except Exception:
            if owner_session is None and session is not None:
                session.rollback()
            raise
        finally:
            if owner_session is not None:
                owner_session.close()

    def run(self, job_id: int) -> ParseResult:
        """Execute a single parse job by ID. Updates parse_jobs and creates parse_artifacts."""
        with Session(self.engine) as session:
            job = session.execute(
                select(ParseJob).where(ParseJob.id == job_id).options(joinedload(ParseJob.file))
            ).scalar_one_or_none()

            if job is None:
                raise ValueError(f"Parse job {job_id} not found")
            if job.status != "queued":
                raise ValueError(f"Parse job {job_id} is not queued (status={job.status})")

            file_rec = job.file
            if file_rec is None or file_rec.local_relpath is None:
                raise ValueError(f"File for job {job_id} not found")

            file_path = self.settings.data_root / file_rec.local_relpath
            if not file_path.is_file():
                job.status = "failed"
                job.error_code = "file_missing"
                session.commit()
                raise FileNotFoundError(f"File not found: {file_path}")

            # Increment attempts
            job.attempts += 1

            # Preflight
            pref = preflight(file_path)

            # Determine output directory
            output_base = self._get_output_base()
            item_id = file_rec.oa_item_id
            item = session.execute(
                select(OAItem).where(OAItem.id == item_id)
            ).scalar_one_or_none()
            content = session.get(ContentObject, file_rec.content_object_id) if file_rec.content_object_id else None
            if content is None:
                raise RuntimeError(f"File {file_rec.id} has no content object")
            output_dir = output_base / str(content.id) / f"artifact-{job.id}"

            # Execute parse
            try:
                if job.engine == "markitdown":
                    result = parse_with_markitdown(file_path, output_dir=output_dir)
                elif job.engine == "mineru":
                    if not mineru_available(self.settings):
                        raise RuntimeError("MinerU is unavailable; refusing to use a fallback parser")
                    result = parse_with_mineru(file_path, self.settings, output_dir=output_dir)
                else:
                    raise ValueError(f"Unsupported parser engine: {job.engine}")
            except RuntimeError as exc:
                if "encrypted" in str(exc):
                    job.status = "failed"
                    job.error_code = "encrypted_document"
                    job.output_relpath = None
                    session.commit()
                    raise
                if "corrupted" in str(exc):
                    job.status = "failed"
                    job.error_code = "corrupted_file"
                    job.output_relpath = None
                    session.commit()
                    raise
                raise

            # Save quality info
            quality_json = output_dir / f"{safe_filename(file_path.stem)}_quality.json"
            quality_data = {
                "quality_score": result.quality_score,
                "warnings": result.warnings,
                "text_length": result.text_length,
                "chinese_char_ratio": result.chinese_char_ratio,
                "replacement_char_ratio": result.replacement_char_ratio,
                "table_count": result.table_count,
                "image_count": result.image_count,
                "preflight": {k: v for k, v in pref.items() if isinstance(v, (int, float, bool, str))},
            }
            quality_json.write_text(json.dumps(quality_data, ensure_ascii=False, indent=2), encoding="utf-8")

            # Update job
            job.status = "completed"
            job.quality_score = result.quality_score
            job.output_relpath = str(result.output_path.relative_to(output_base))
            job.config_hash = result.config_hash

            # Update item pipeline status
            item = session.execute(
                select(OAItem).where(OAItem.id == item_id)
            ).scalar_one_or_none()
            if item and item.pipeline_status == PipelineStatus.PARSE_QUEUED:
                item.pipeline_status = PipelineStatus.PARSED

            # Create parse artifact record
            source_sha = file_rec.sha256 or sha256_file(file_path)
            product_sha = sha256_file(result.output_path) if result.output_path.is_file() else None

            artifact = ParseArtifact(
                parse_job_id=job.id,
                content_object_id=content.id,
                engine=result.engine,
                engine_version=result.engine_version,
                output_relpath=str(result.output_path.relative_to(output_base)),
                source_sha256=source_sha,
                product_sha256=product_sha,
                config_hash=result.config_hash,
                page_map_json=json.dumps(pref, ensure_ascii=False),
                quality_score=result.quality_score,
                quality_status="low" if result.quality_score < 0.5 else "ok",
                lifecycle_status="candidate",
            )
            session.add(artifact)
            session.flush()

            current = session.get(ParseArtifact, content.active_parse_artifact_id) if content.active_parse_artifact_id else None
            if result.quality_score >= 0.5 and (current is None or (current.quality_score or 0) <= result.quality_score):
                if current is not None:
                    current.lifecycle_status = "superseded"
                artifact.lifecycle_status = "valid"
                content.active_parse_artifact_id = artifact.id
                documents = session.execute(
                    select(KnowledgeDocument).where(KnowledgeDocument.content_object_id == content.id)
                ).scalars()
                for document in documents:
                    document.active_parse_artifact_id = artifact.id
            else:
                artifact.lifecycle_status = "rejected"
            session.commit()

            return result

    def run_all_pending(self, limit: int = 50) -> dict:
        """Process all queued parse jobs up to limit.

        Returns summary dict with processed, succeeded, failed counts.
        """
        summary = {"processed": 0, "succeeded": 0, "failed": 0, "errors": []}

        with Session(self.engine) as session:
            jobs = (
                session.query(ParseJob)
                .where(ParseJob.status == "queued")
                .order_by(ParseJob.id)
                .limit(limit)
                .all()
            )

        for job in jobs:
            try:
                self.run(job.id)
                summary["succeeded"] += 1
            except Exception as exc:
                summary["failed"] += 1
                summary["errors"].append(f"job={job.id}: {type(exc).__name__}: {exc}")
            summary["processed"] += 1

        return summary

    def _engine_version(self, engine: str) -> str:
        if engine == "markitdown":
            try:
                import markitdown as md
                return getattr(md, "__version__", "unknown")
            except ImportError:
                return "unknown"
        if engine == "mineru":
            return "0.1.0"
        return "unknown"
