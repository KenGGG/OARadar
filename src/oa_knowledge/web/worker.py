from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.archive import atomic_write_bytes
from oa_knowledge.constants import BatchStatus, LEASE_TTL
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import ArchivedFile, BatchItem, CollectionBatch, ExclusionPolicy, ItemSnapshot, OAItem, OAManifestItem, OperationEvent, OperationJob, ParseJob, PipelineTask, ReviewEntry, SourceAttachment
from oa_knowledge.production_pipeline import ProductionQueue
from oa_knowledge.ops.audit import audit_database
from oa_knowledge.ops.capacity import capacity_report
from oa_knowledge.web.cli_runner import run_cli
from oa_knowledge.web.status import _apply_policy_to_pending, execute_archive_job


def _pump_stream(stream, buffer: list[str]) -> None:
    try:
        for line in stream:
            buffer.append(line)
    finally:
        stream.close()


def _run_piped(command: list[str], cwd: Path, poll_callback, poll_interval: float = 5.0):
    """Run a subprocess with PIPEd stdout/stderr.

    The pipes are drained on background threads so a chatty child cannot fill the
    OS pipe buffer (~64KB) and deadlock the parent while it polls. Returns
    ``(returncode, stdout, stderr)``.
    """
    process = subprocess.Popen(
        command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    stdout_buf: list[str] = []
    stderr_buf: list[str] = []
    out_thread = threading.Thread(target=_pump_stream, args=(process.stdout, stdout_buf), daemon=True)
    err_thread = threading.Thread(target=_pump_stream, args=(process.stderr, stderr_buf), daemon=True)
    out_thread.start()
    err_thread.start()
    while process.poll() is None:
        poll_callback()
        time.sleep(poll_interval)
    process.wait()
    out_thread.join()
    err_thread.join()
    return process.returncode, "".join(stdout_buf), "".join(stderr_buf)


class OperationWorker:
    """Single durable worker for Web-enqueued OA read-only operations."""

    def __init__(self, settings: Settings, config_path: Path | None = None) -> None:
        self.settings = settings
        self.config_path = config_path
        self.owner = f"worker-{os.getpid()}"
        self.engine = create_db_engine(settings.database_path)
        self.production_queue = ProductionQueue(self.engine)

    def close(self) -> None:
        self.engine.dispose()
        (self.settings.data_root / "runtime" / "operation-worker.json").unlink(missing_ok=True)

    @staticmethod
    def _retry_progress(total_targets: int, resumed: int, completed_after_resume: int) -> int:
        return min(total_targets, resumed + completed_after_resume)

    def recover_expired(self) -> int:
        now = datetime.now(timezone.utc)
        with Session(self.engine) as session:
            candidates = session.scalars(select(OperationJob).where(OperationJob.status.in_(('running', 'queued')))).all()
            jobs = [job for job in candidates if (
                job.lease_expires_at is None
                or (job.lease_expires_at.replace(tzinfo=timezone.utc) if job.lease_expires_at.tzinfo is None else job.lease_expires_at) < now
                or not self._lease_owner_alive(job.lease_owner)
            )]
            for job in jobs:
                if job.status == "running":
                    job.status = "queued"
                job.lease_owner = None
                job.lease_expires_at = None
                job.last_error_code = "recovered_expired_lease"
                self._event(session, job, "recovered", "queued", {"reason": "expired_lease"})
            session.commit()
            operation_count = len(jobs)
        from oa_knowledge.resources import ResourceCoordinator
        lease_count = ResourceCoordinator(self.engine).recover_dead_owners(self._lease_owner_alive)
        return operation_count + self.production_queue.recover_abandoned(self._lease_owner_alive) + lease_count

    @staticmethod
    def _lease_owner_alive(owner: str | None) -> bool:
        if not owner or not owner.startswith("worker-"):
            return False
        try:
            pid = int(owner.removeprefix("worker-"))
            command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except (ValueError, OSError):
            return False
        return "oa" in command and "worker" in command

    def run_once(self) -> bool:
        job_id = self._claim_next()
        if job_id is None:
            task = self.production_queue.claim(self.owner)
            if task is None:
                return False
            self._execute_pipeline_task(task)
            return True
        with Session(self.engine) as session:
            job = session.get(OperationJob, job_id)
            if job is None:
                return False
            job_type = job.job_type
        try:
            if job_type == "archive_batch":
                execute_archive_job(self.settings, job_id, self.config_path)
            elif job_type == "discovery":
                self._execute_discovery(job_id)
            elif job_type == "backfill_campaign":
                self._execute_backfill_campaign(job_id)
            elif job_type == "full_manifest":
                self._execute_full_manifest(job_id)
            elif job_type == "full_manifest_retry":
                self._execute_full_manifest_retry(job_id)
            elif job_type == "done_incremental":
                self._execute_done_incremental(job_id)
            elif job_type == "online_audit":
                self._execute_online_audit(job_id)
            elif job_type == "archive_date_reconcile":
                self._execute_archive_date_reconcile(job_id)
            else:
                self._finish(job_id, "failed", "unsupported_job_type")
        except Exception as exc:
            self._finish(job_id, "failed", f"worker_exception_{type(exc).__name__}")
        return True

    def _execute_archive_date_reconcile(self, job_id: int) -> None:
        from oa_knowledge.archive_reconciliation import reconcile_item
        from oa_knowledge.db.models import OAItem
        with Session(self.engine) as session:
            job = session.get(OperationJob, job_id)
            if not job or job.status not in {"queued", "running"}:
                return
            job.status = "running"; job.started_at = job.started_at or datetime.now(timezone.utc); session.commit()
        with Session(self.engine) as session:
            ids = list(session.scalars(select(OAItem.id).where(OAItem.source_channel == "done", OAItem.archive_relpath.is_not(None)).order_by(OAItem.id)))
        migrated = failed = processed = 0
        for item_id in ids:
            with Session(self.engine) as session:
                job = session.get(OperationJob, job_id)
                if not job or job.status == "paused":
                    return
                try:
                    result = reconcile_item(session, self.settings, item_id)
                    session.commit(); migrated += result.status == "migrated"
                except (FileNotFoundError, FileExistsError, ValueError) as exc:
                    session.rollback(); failed += 1
                    job = session.get(OperationJob, job_id); job.last_error_code = type(exc).__name__.upper(); session.commit()
                processed += 1
                job = session.get(OperationJob, job_id)
                job.parameters_json = json.dumps({"processed": processed, "total": len(ids), "migrated": migrated, "failed": failed})
                job.heartbeat_at = datetime.now(timezone.utc); session.commit()
        self._finish(job_id, "completed", None)

    def _execute_online_audit(self, job_id: int) -> None:
        from oa_knowledge.collector.browser import BrowserSession, LoginState
        from oa_knowledge.collector.detail import CollaborationDetailAdapter
        from oa_knowledge.cli import verified_attachment_resolver
        from oa_knowledge.db.models import OnlineAuditRun
        from oa_knowledge.detail_archive import archive_collaboration_detail
        from oa_knowledge.full_manifest import archive_proxy
        from oa_knowledge.online_audit import ATTACHMENT_ROLES, AuditObservation, canonical_downloaded_count, execute_audit, fail_audit, unique_capture_attachment_count
        with Session(self.engine) as session:
            run = session.scalar(select(OnlineAuditRun).where(OnlineAuditRun.job_id == job_id))
            if run is None:
                self._finish(job_id, "failed", "audit_run_not_found"); return
            run_id = run.id
            job = session.get(OperationJob, job_id)
            if job:
                job.status = "running"; job.started_at = job.started_at or datetime.now(timezone.utc)
                session.commit()
        with BrowserSession(self.settings, headed=False) as browser:
            if browser.login_with_saved_credentials(30) != LoginState.AUTHENTICATED:
                fail_audit(self.settings, run_id, "OA_AUTH_EXPIRED")
                self._finish(job_id, "failed", "OA_AUTH_EXPIRED")
                return
            if browser.page is None:
                raise RuntimeError("browser page is not available")
            inventory_adapter = CollaborationDetailAdapter(browser.page, inventory_only=True)
            download_adapter = CollaborationDetailAdapter(
                browser.page,
                attachment_resolver=verified_attachment_resolver(self.engine, self.settings.data_root),
            )

            def inspect(item) -> AuditObservation:
                if not item.workitem_id_text:
                    raise RuntimeError("OA item identifier unavailable")
                capture = inventory_adapter.capture_direct(
                    browser.base_url, item.workitem_id_text,
                    max_depth=self.settings.collector.max_attachment_depth,
                    total_timeout_seconds=self.settings.online_audit.item_timeout_seconds,
                    download_timeout_seconds=self.settings.online_audit.download_timeout_seconds,
                )
                recognized = unique_capture_attachment_count(capture)
                with Session(self.engine) as session:
                    oa = session.scalar(select(OAItem).where(OAItem.oa_item_key == item.oa_item_key))
                    verified_rows = session.scalar(select(func.count()).select_from(ArchivedFile).where(
                        ArchivedFile.oa_item_id == oa.id,
                        ArchivedFile.file_role.in_(ATTACHMENT_ROLES),
                        ArchivedFile.download_status == "verified",
                    )) if oa else 0
                    unique_hashes = session.scalar(select(func.count(func.distinct(ArchivedFile.sha256))).where(
                        ArchivedFile.oa_item_id == oa.id,
                        ArchivedFile.file_role.in_(ATTACHMENT_ROLES), ArchivedFile.download_status == "verified",
                        ArchivedFile.sha256.is_not(None),
                    )) if oa else 0
                    downloaded = canonical_downloaded_count(recognized=recognized, verified_rows=verified_rows or 0, unique_hashes=unique_hashes or 0)
                if recognized > (downloaded or 0):
                    full_capture = download_adapter.capture_direct(
                        browser.base_url, item.workitem_id_text,
                        max_depth=self.settings.collector.max_attachment_depth,
                        total_timeout_seconds=self.settings.collector.attachment_total_timeout_seconds,
                        download_timeout_seconds=self.settings.collector.download_timeout_seconds,
                    )
                    with Session(self.engine) as session:
                        manifest = session.scalar(select(OAManifestItem).where(OAManifestItem.oa_item_key == item.oa_item_key))
                        if manifest is None:
                            raise LookupError("manifest item disappeared during audit repair")
                        archive_collaboration_detail(session, archive_proxy(manifest), full_capture, self.settings.data_root)
                        manifest.archive_relpath = session.scalar(select(OAItem.archive_relpath).where(OAItem.oa_item_key == manifest.oa_item_key))
                        session.commit()
                return AuditObservation(recognized_attachments=recognized)

            try:
                execute_audit(self.settings, run_id, inspect_item=inspect)
            except Exception:
                fail_audit(self.settings, run_id, "AUDIT_WORKER_ERROR")
                raise

    def _execute_done_incremental(self, job_id: int) -> None:
        command = [sys.executable, "-m", "oa_knowledge.cli", "manifest", "refresh-head", "--max-pages", "3"]
        if self.config_path is not None:
            command.extend(("--config", str(self.config_path)))
        with Session(self.engine) as session:
            job = session.get(OperationJob, job_id)
            if job is None:
                return
            job.status = "running"
            job.started_at = job.started_at or datetime.now(timezone.utc)
            session.commit()
        result = subprocess.run(command, cwd=Path.cwd(), capture_output=True, text=True)
        self._finish(job_id, "completed" if result.returncode == 0 else "failed", None if result.returncode == 0 else f"incremental_exit_{result.returncode}")

    def _execute_pipeline_task(self, task: PipelineTask) -> None:
        try:
            if task.stage == "attachment_inventory":
                self._pipeline_attachment_inventory(task)
            elif task.stage == "parse":
                self._pipeline_parse(task)
            elif task.stage == "detail_sync" and task.queue_name == "realtime_pending":
                self._pipeline_pending_detail(task)
            elif task.stage == "pending_summary":
                self._pipeline_pending_summary(task)
            elif task.stage == "pending_parse":
                self._pipeline_pending_parse(task)
            elif task.stage == "ollama_extract":
                self._pipeline_done_knowledge(task)
            else:
                self.production_queue.fail(task.id, self.owner, "PIPELINE_STAGE_NOT_IMPLEMENTED", task.stage, recoverable=False)
        except Exception as exc:
            code = {
                "FileNotFoundError": "ATTACHMENT_DOWNLOAD_FAILED",
                "RuntimeError": "PIPELINE_RESOURCE_BUSY",
                "DoneKnowledgeError": "OLLAMA_SCHEMA_INVALID",
                "PendingSummaryError": "OLLAMA_SCHEMA_INVALID",
            }.get(type(exc).__name__, "PIPELINE_TASK_FAILED")
            self.production_queue.fail(task.id, self.owner, code, type(exc).__name__, recoverable=True)

    def _pipeline_attachment_inventory(self, task: PipelineTask) -> None:
        from oa_knowledge.pipeline import ParsePipeline
        with Session(self.engine) as session:
            item = session.scalar(select(OAItem).where(OAItem.oa_item_key == task.logical_item_key))
            if item is None:
                raise FileNotFoundError("archived item is missing")
            files = session.scalars(select(ArchivedFile).where(
                ArchivedFile.oa_item_id == item.id,
                ArchivedFile.download_status == "verified",
                ArchivedFile.local_relpath.is_not(None),
            ).order_by(ArchivedFile.id)).all()
            files = self._historical_source_files(files)
            pipeline = ParsePipeline(self.settings, self.engine)
            enqueued = sum(pipeline.enqueue(file.id, session=session) is not None for file in files)
        if not files:
            self.production_queue.complete(task.id, self.owner)
            return
        self.production_queue.advance(task.id, self.owner, "parse", progress_current=0, progress_total=enqueued)

    @staticmethod
    def _historical_source_files(files):
        source_roles = {
            "direct_attachment", "official_body", "official_attachment",
            "associated_document", "opinion_attachment",
        }
        return [
            file for file in files
            if file.file_role in source_roles
        ]

    def _pipeline_parse(self, task: PipelineTask) -> None:
        from oa_knowledge.pipeline import ParsePipeline
        with Session(self.engine) as session:
            item = session.scalar(select(OAItem).where(OAItem.oa_item_key == task.logical_item_key))
            if item is None:
                raise FileNotFoundError("archived item is missing")
            files = session.scalars(select(ArchivedFile).where(ArchivedFile.oa_item_id == item.id).order_by(ArchivedFile.id)).all()
            source_ids = [file.id for file in self._historical_source_files(files)]
            jobs = session.scalars(select(ParseJob).where(ParseJob.file_id.in_(source_ids)).order_by(ParseJob.id)).all() if source_ids else []
            queued = next((job for job in jobs if job.status == "queued"), None)
            completed = sum(job.status == "completed" for job in jobs)
            failed = sum(job.status == "failed" for job in jobs)
        if queued is not None:
            try:
                ParsePipeline(self.settings, self.engine).run(queued.id)
            except Exception as exc:
                if not self._handle_nonfatal_parse_error(queued.id, exc):
                    raise
            self.production_queue.advance(task.id, self.owner, "parse", progress_current=completed + 1, progress_total=len(jobs))
            return
        if failed:
            self.production_queue.fail(task.id, self.owner, "PARSE_FAILED", f"{failed} parse jobs failed", recoverable=True)
            return
        self.production_queue.advance(task.id, self.owner, "ollama_extract", progress_current=completed, progress_total=len(jobs))

    @staticmethod
    def _nonfatal_parse_error(exc: Exception) -> bool:
        return type(exc).__name__ in {"UnsupportedFormatException", "FileConversionException"}

    def _handle_nonfatal_parse_error(self, job_id: int, exc: Exception) -> bool:
        if not self._nonfatal_parse_error(exc):
            return False
        with Session(self.engine) as session:
            job = session.get(ParseJob, job_id)
            if job is not None:
                job.status = "skipped"
                job.error_code = "unsupported_format"
                session.commit()
        return True

    def _pipeline_pending_detail(self, task: PipelineTask) -> None:
        from oa_knowledge.collector.browser import BrowserSession, LoginState
        from oa_knowledge.collector.detail import CollaborationDetailAdapter
        from oa_knowledge.collector.pending import PendingAdapter
        from oa_knowledge.db.models import ItemOccurrence
        from oa_knowledge.pending_archive import persist_pending_capture
        from oa_knowledge.resources import ResourceCoordinator
        payload = json.loads(task.payload_json or "{}")
        with Session(self.engine) as session:
            occurrence = session.get(ItemOccurrence, int(payload["occurrence_id"]))
            if occurrence is None or not occurrence.affair_id_text:
                self.production_queue.fail(task.id, self.owner, "PENDING_IDENTITY_INCOMPLETE", "missing affair id", recoverable=False)
                return
            occurrence_key, affair_id = occurrence.occurrence_key, occurrence.affair_id_text
        coordinator = ResourceCoordinator(self.engine); lease_owner = f"{self.owner}:pending:{task.id}"
        lease = coordinator.acquire("oa_browser", lease_owner, ttl_seconds=600, uses_local_gpu=False)
        if lease is None:
            raise RuntimeError("OA browser is busy")
        try:
            with BrowserSession(self.settings, headed=False) as browser:
                if browser.login_with_saved_credentials(30) != LoginState.AUTHENTICATED:
                    self.production_queue.fail(task.id, self.owner, "OA_AUTH_EXPIRED", "saved OA session is not authenticated", recoverable=True)
                    return
                if browser.page is None:
                    raise RuntimeError("browser page is not available")
                capture = CollaborationDetailAdapter(browser.page).capture(
                    affair_id, max_depth=self.settings.collector.max_attachment_depth,
                    total_timeout_seconds=self.settings.collector.attachment_total_timeout_seconds,
                    download_timeout_seconds=self.settings.collector.download_timeout_seconds,
                    direct_url=PendingAdapter.detail_url(browser.base_url, affair_id),
                )
            with Session(self.engine) as session:
                persist_pending_capture(session, occurrence_key, capture, self.settings.data_root); session.commit()
        finally:
            coordinator.release(lease, lease_owner)
        self.production_queue.advance(task.id, self.owner, "pending_parse")

    def _pipeline_pending_parse(self, task: PipelineTask) -> None:
        from oa_knowledge.pipeline import ParsePipeline
        with Session(self.engine) as session:
            snapshot = session.scalar(select(ItemSnapshot).where(
                ItemSnapshot.logical_item_id == task.logical_item_id,
            ).order_by(ItemSnapshot.id.desc()).limit(1))
            source_ids = list(session.scalars(select(SourceAttachment.source_file_id).where(
                SourceAttachment.snapshot_id == snapshot.id,
                SourceAttachment.source_file_id.is_not(None),
                SourceAttachment.download_status == "verified",
            ))) if snapshot else []
            pipeline = ParsePipeline(self.settings, self.engine)
            for file_id in source_ids:
                pipeline.enqueue(file_id, session=session)
            session.commit()
            jobs = session.scalars(select(ParseJob).where(ParseJob.file_id.in_(source_ids)).order_by(ParseJob.id)).all() if source_ids else []
            queued = next((job for job in jobs if job.status == "queued"), None)
            failed = sum(job.status == "failed" for job in jobs)
        if queued is not None:
            try:
                ParsePipeline(self.settings, self.engine).run(queued.id)
            except Exception as exc:
                if not self._handle_nonfatal_parse_error(queued.id, exc):
                    raise
            self.production_queue.advance(task.id, self.owner, "pending_parse")
            return
        if failed:
            self.production_queue.fail(task.id, self.owner, "PARSE_FAILED", f"{failed} pending parse jobs failed", recoverable=True)
            return
        self.production_queue.advance(task.id, self.owner, "pending_summary")

    def _pipeline_pending_summary(self, task: PipelineTask) -> None:
        from oa_knowledge.pending_summary import summarize_pending
        summarize_pending(self.settings, self.engine, int(task.logical_item_key))
        # Notification delivery is intentionally decoupled from the OA capture
        # pipeline. A requested notification must not advance into an
        # unsupported stage and turn a valid local summary into a failed task.
        self.production_queue.complete(task.id, self.owner)

    def _pipeline_done_knowledge(self, task: PipelineTask) -> None:
        from oa_knowledge.done_knowledge import NoAttachmentEvidence, generate_done_knowledge
        try:
            generate_done_knowledge(self.settings, self.engine, task.logical_item_key)
        except NoAttachmentEvidence:
            self.production_queue.complete(task.id, self.owner)
            return
        except LookupError as exc:
            self.production_queue.fail(task.id, self.owner, "PARSE_QUALITY_REJECTED", type(exc).__name__, recoverable=False)
            return
        self.production_queue.complete(task.id, self.owner)

    def _execute_full_manifest(self, job_id: int) -> None:
        """Run the page pipeline while publishing durable DB progress to Web."""
        with Session(self.engine) as session:
            job = session.get(OperationJob, job_id)
            if job is None or job.status not in {"queued", "running"}:
                return
            job.status = "running"
            job.started_at = job.started_at or datetime.now(timezone.utc)
            job.heartbeat_at = datetime.now(timezone.utc)
            job.lease_expires_at = job.heartbeat_at + LEASE_TTL
            job.progress_current = session.scalar(select(func.count()).select_from(OAManifestItem)) or 0
            job.progress_total = 0
            self._event(session, job, "manifest_started", "running", {})
            session.commit()
        command = [sys.executable, "-m", "oa_knowledge.cli", "manifest", "download", "--max-items", "10000"]
        if self.config_path is not None:
            command.extend(("--config", str(self.config_path)))

        def heartbeat() -> None:
            with Session(self.engine) as session:
                job = session.get(OperationJob, job_id)
                if job is None:
                    return
                job.progress_current = session.scalar(select(func.count()).select_from(OAManifestItem)) or 0
                job.progress_total = 0
                job.heartbeat_at = datetime.now(timezone.utc)
                job.lease_expires_at = job.heartbeat_at + LEASE_TTL
                session.commit()

        returncode, stdout, _stderr = _run_piped(command, Path.cwd(), heartbeat, poll_interval=5.0)
        payload = {}
        for line in reversed(stdout.splitlines()):
            try:
                payload = json.loads(line); break
            except json.JSONDecodeError:
                continue
        with Session(self.engine) as session:
            job = session.get(OperationJob, job_id)
            if job is None: return
            job.progress_current = session.scalar(select(func.count()).select_from(OAManifestItem)) or 0
            job.progress_total = payload.get("oa_total_count")
            job.last_error_code = None if returncode == 0 else (payload.get("manifest_status") or f"exit_{returncode}")
            job.status = "completed" if returncode == 0 else "failed"
            job.finished_at = datetime.now(timezone.utc); job.lease_owner = None; job.lease_expires_at = None
            self._event(session, job, "manifest_finished", job.status, {
                "manifest_status": payload.get("manifest_status"), "progress_current": job.progress_current,
            })
            session.commit()

    def _execute_full_manifest_retry(self, job_id: int) -> None:
        with Session(self.engine) as session:
            initial_job = session.get(OperationJob, job_id)
            initial_params = json.loads(initial_job.parameters_json or "{}") if initial_job else {}
        if initial_params.get("source_status") == "audit_all":
            refresh_command = [sys.executable, "-m", "oa_knowledge.cli", "manifest", "refresh-head"]
            if self.config_path is not None:
                refresh_command.extend(("--config", str(self.config_path)))
            refreshed = subprocess.run(refresh_command, cwd=Path.cwd(), capture_output=True, text=True)
            if refreshed.returncode != 0:
                with Session(self.engine) as session:
                    job = session.get(OperationJob, job_id)
                    if job is not None:
                        self._set_failed(session, job, f"manifest_head_refresh_exit_{refreshed.returncode}")
                        session.commit()
                return
            with Session(self.engine) as session:
                job = session.get(OperationJob, job_id)
                if job is None:
                    return
                refreshed_keys = list(session.scalars(select(OAManifestItem.oa_item_key).order_by(OAManifestItem.id)))
                params = json.loads(job.parameters_json or "{}")
                params["oa_item_keys"] = refreshed_keys
                job.parameters_json = json.dumps(params, ensure_ascii=False)
                job.progress_current = 0
                job.progress_total = len(refreshed_keys)
                session.commit()
        with Session(self.engine) as session:
            job = session.get(OperationJob, job_id)
            if job is None or job.status not in {"queued", "running"}:
                return
            params = json.loads(job.parameters_json or "{}")
            keys = list(params.get("oa_item_keys") or [])
            total_targets = len(keys)
            source_status = params.get("source_status", "download_failed")
            job.status = "running"
            # A recovered job resumes from its durable item offset. Reset the
            # timing window so progress only counts rows completed by this run.
            job.started_at = datetime.now(timezone.utc)
            job.progress_current = min(job.progress_current or 0, len(keys))
            job.progress_total = total_targets
            job.heartbeat_at = datetime.now(timezone.utc)
            job.lease_expires_at = job.heartbeat_at + LEASE_TTL
            self._event(session, job, "manifest_retry_started", "running", {"target_count": len(keys)})
            session.commit()
        if not keys:
            self._finish(job_id, "completed")
            return
        processed = 0
        with Session(self.engine) as session:
            job = session.get(OperationJob, job_id)
            resume_at = min(job.progress_current if job else 0, len(keys))
        if resume_at:
            keys = keys[resume_at:]
            processed = resume_at
        last_error: str | None = None
        command = [sys.executable, "-m", "oa_knowledge.cli", "manifest", "download", "--max-items", str(len(keys))]
        if source_status != "audit_all":
            command.extend(("--item-ids", ",".join(keys)))
        if source_status == "download_failed":
            command.append("--failed-only")
        elif source_status == "no_attachment":
            command.append("--recheck-no-attachment")
        elif source_status == "audit_all":
            command.append("--audit-all")
        if self.config_path is not None:
            command.extend(("--config", str(self.config_path)))
        def heartbeat() -> None:
            with Session(self.engine) as session:
                job = session.get(OperationJob, job_id)
                if job is None:
                    return
                completed = session.scalar(select(func.count()).select_from(OAManifestItem).where(
                    OAManifestItem.oa_item_key.in_(keys),
                    OAManifestItem.last_retry_at.is_not(None),
                    OAManifestItem.last_retry_at >= (job.started_at or datetime.now(timezone.utc)),
                    OAManifestItem.processing_status != "processing",
                )) or 0
                active = session.scalar(select(OAManifestItem).where(OAManifestItem.processing_status == "processing").order_by(OAManifestItem.last_retry_at.desc(), OAManifestItem.id.desc()).limit(1))
                job.progress_current = self._retry_progress(total_targets, processed, completed)
                job.heartbeat_at = datetime.now(timezone.utc)
                job.lease_expires_at = job.heartbeat_at + LEASE_TTL
                if active:
                    self._event(session, job, "manifest_retry_item_started", "running", {"oa_item_key": active.oa_item_key, "manifest_id": active.id, "item_id": active.workitem_id_text or active.oa_item_key, "title": active.title, "stage": "正在进入 OA 详情页并识别附件"})
                session.commit()

        _returncode, _stdout, stderr = _run_piped(command, Path.cwd(), heartbeat, poll_interval=2.0)
        if _returncode != 0:
            last_error = f"retry_exit_{_returncode}"
        processed = total_targets
        with Session(self.engine) as session:
            job = session.get(OperationJob, job_id)
            if job is None:
                return
            job.status = "completed" if last_error is None else "failed"
            job.last_error_code = last_error
            job.finished_at = datetime.now(timezone.utc)
            job.lease_owner = None
            job.lease_expires_at = None
            if last_error is not None:
                stale_rows = session.scalars(select(OAManifestItem).where(OAManifestItem.processing_status == "processing")).all()
                for row in stale_rows:
                    row.processing_status = "download_failed"
                    row.failure_stage = "interrupted"
                    row.last_error = last_error
                    row.last_retry_at = datetime.now(timezone.utc)
            self._event(session, job, "manifest_retry_finished", job.status, {
                "processed": processed,
                "stderr_tail": (stderr or "")[-2000:] if last_error is not None else "",
            })
            session.commit()

    def run_forever(self, poll_seconds: float = 2.0) -> None:
        self.recover_expired()
        while True:
            self._write_runtime_status()
            if not self.run_once():
                time.sleep(poll_seconds)

    def _write_runtime_status(self) -> None:
        payload = json.dumps({
            "owner": self.owner,
            "pid": os.getpid(),
            "status": "idle",
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        }).encode("utf-8")
        destination = atomic_write_bytes(payload, self.settings.data_root, "runtime/operation-worker.json")
        os.chmod(destination, 0o600)

    def _claim_next(self) -> int | None:
        now = datetime.now(timezone.utc)
        with Session(self.engine) as session:
            priority = case(
                (and_(OperationJob.job_type == "full_manifest_retry", OperationJob.parameters_json.like('%"source_status": "download_failed"%')), 0),
                (OperationJob.job_type == "full_manifest_retry", 1),
                else_=2,
            )
            job = session.scalar(select(OperationJob).where(
                OperationJob.status == "queued",
                or_(OperationJob.lease_expires_at.is_(None), OperationJob.lease_expires_at < now, OperationJob.lease_owner == self.owner),
            ).order_by(priority, OperationJob.id.desc()).limit(1))
            if job is None:
                return None
            job.lease_owner = self.owner
            job.lease_expires_at = now + LEASE_TTL
            job.heartbeat_at = now
            session.commit()
            return job.id

    def _execute_discovery(self, job_id: int) -> None:
        with Session(self.engine) as session:
            job = session.get(OperationJob, job_id)
            if job is None or job.status != "queued" or job.lease_owner != self.owner:
                return
            parameters = json.loads(job.parameters_json)
            batch = session.get(CollectionBatch, int(parameters["batch_id"]))
            if batch is None:
                self._set_failed(session, job, "batch_not_found")
                session.commit()
                return
            if batch.frozen_at is None:
                if batch.status != BatchStatus.PLANNED:
                    self._set_failed(session, job, f"cannot_freeze_{batch.status}")
                    session.commit()
                    return
                batch.frozen_at = datetime.now(timezone.utc)
            job.status = "running"
            job.started_at = job.started_at or datetime.now(timezone.utc)
            job.heartbeat_at = datetime.now(timezone.utc)
            self._event(session, job, "started", "running", {"batch_key": batch.batch_key})
            batch_key = batch.batch_key
            session.commit()

        command = [sys.executable, "-m", "oa_knowledge.cli", "batch", "discover", batch_key]
        if self.config_path is not None:
            command.extend(("--config", str(self.config_path)))
        try:
            result = subprocess.run(
                command, cwd=Path.cwd(), capture_output=True, text=True,
                timeout=1800, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._finish(job_id, "failed", type(exc).__name__)
            return

        with Session(self.engine) as session:
            job = session.get(OperationJob, job_id)
            if job is None:
                return
            payload: dict = {}
            for line in reversed(result.stdout.splitlines()):
                try:
                    payload = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
            if result.returncode == 3:
                status, error = "auth_required", "auth_required"
            elif result.returncode != 0:
                status, error = "failed", f"exit_{result.returncode}"
            else:
                status, error = "completed", None
                for policy in session.scalars(select(ExclusionPolicy).where(ExclusionPolicy.enabled.is_(True))).all():
                    _apply_policy_to_pending(session, policy)
            job.status = status
            job.last_error_code = error
            job.progress_current = int(payload.get("discovered_count", 0))
            job.progress_total = int(payload.get("planned_limit", job.progress_total or 0))
            job.finished_at = datetime.now(timezone.utc)
            job.heartbeat_at = job.finished_at
            job.lease_owner = None
            job.lease_expires_at = None
            self._event(session, job, "finished", status, {"discovered": job.progress_current})
            session.commit()

    def _execute_backfill_campaign(self, job_id: int) -> None:
        """Run all Stage 2A-7 windows serially, stopping at every safety gate."""
        with Session(self.engine) as session:
            job = session.get(OperationJob, job_id)
            if job is None or job.status != "queued" or job.lease_owner != self.owner:
                return
            parameters = json.loads(job.parameters_json)
            job.status = "running"
            job.started_at = job.started_at or datetime.now(timezone.utc)
            job.progress_current = session.scalar(select(func.count()).select_from(BatchItem).join(CollectionBatch).where(
                CollectionBatch.notes.like("backfill:v1%"),
                BatchItem.archive_status.in_(("archived", "confirmed_skip", "review_required")),
            )) or 0
            self._event(session, job, "started", "running", parameters)
            session.commit()

        consecutive_runner_failures = 0
        while True:
            with Session(self.engine) as session:
                job = session.get(OperationJob, job_id)
                if job is None or job.status != "running":
                    return
                batch = session.scalar(
                    select(CollectionBatch)
                    .where(CollectionBatch.notes.like("backfill:v1%"), CollectionBatch.status != BatchStatus.COMPLETED)
                    .order_by(CollectionBatch.window_start.desc(), CollectionBatch.id.desc()).limit(1)
                )
                if batch is not None:
                    self._resolve_recovered_collection_issues(session, batch.id)
                    failed_items = session.scalars(select(BatchItem).where(
                        BatchItem.batch_id == batch.id,
                        BatchItem.archive_status.in_(("collect_failed", "download_failed")),
                    )).all()
                    pending = session.scalar(select(func.count()).select_from(BatchItem).where(
                        BatchItem.batch_id == batch.id,
                        BatchItem.archive_status.in_(("pending", "archiving")),
                    )) or 0
                    if failed_items:
                        self._record_collection_issues(session, batch.id)
                        for item in failed_items:
                            if item.retry_count < 2:
                                item.archive_status = "pending"
                                item.last_error = None
                            else:
                                item.archive_status = "review_required"
                        self._event(session, job, "items_quarantined", "running", {
                            "batch_id": batch.id,
                            "retrying": sum(item.retry_count < 2 for item in failed_items),
                            "review_required": sum(item.retry_count >= 2 for item in failed_items),
                        })
                        session.flush()
                        archived = session.scalar(select(func.count()).select_from(BatchItem).where(
                            BatchItem.batch_id == batch.id, BatchItem.archive_status == "archived",
                        )) or 0
                        skipped = session.scalar(select(func.count()).select_from(BatchItem).where(
                            BatchItem.batch_id == batch.id, BatchItem.archive_status == "confirmed_skip",
                        )) or 0
                        reviewed = session.scalar(select(func.count()).select_from(BatchItem).where(
                            BatchItem.batch_id == batch.id, BatchItem.archive_status == "review_required",
                        )) or 0
                        active_failed = session.scalar(select(func.count()).select_from(BatchItem).where(
                            BatchItem.batch_id == batch.id,
                            BatchItem.archive_status.in_(("collect_failed", "download_failed")),
                        )) or 0
                        batch.archived_count = archived
                        batch.skipped_count = skipped
                        batch.failed_count = reviewed + active_failed
                        if archived + skipped + reviewed == batch.discovered_count:
                            batch.status = BatchStatus.COMPLETED
                            batch.finished_at = datetime.now(timezone.utc)
                            pending = 0
                            batch_key = batch.batch_key
                        session.commit()
                        if batch.status != BatchStatus.COMPLETED:
                            continue
                    if pending and batch.status == BatchStatus.PAUSED:
                        batch.status = BatchStatus.RUNNING
                    batch_key = batch.batch_key
                else:
                    pending, batch_key = 0, None
                self._heartbeat(session, job)
                session.commit()

            if batch_key is not None and pending:
                result, payload = self._run_cli([
                    "batch", "run", batch_key,
                    "--max-items", str(parameters["chunk_size"]),
                    "--time-budget-seconds", str(parameters["time_budget_seconds"]),
                    "--operation-job-id", str(job_id),
                ], int(parameters["time_budget_seconds"]) + 120)
                if result == 3 or payload.get("run_status") == "auth_required":
                    self._finish(job_id, "auth_required", "auth_required")
                    return
                if result != 0:
                    consecutive_runner_failures += 1
                    self._record_runner_retry(job_id, result, payload, consecutive_runner_failures)
                    if consecutive_runner_failures <= 3:
                        time.sleep(min(2 ** (consecutive_runner_failures - 1), 4))
                        continue
                    self._finish(job_id, "failed", str(payload.get("run_status") or payload.get("reason") or f"exit_{result}"))
                    return
                consecutive_runner_failures = 0
                self._record_campaign_progress(job_id, "chunk_finished", payload)
                if payload.get("run_status") in {"completed", "completed_with_issues"}:
                    error = self._validate_campaign_batch(job_id, batch_key)
                    if error:
                        self._finish(job_id, "failed", error)
                        return
                continue

            if batch_key is not None:
                error = self._validate_campaign_batch(job_id, batch_key)
                if error:
                    self._finish(job_id, "failed", error)
                    return

            result, payload = self._run_cli([
                "backfill", "next", "--from", parameters["from_date"], "--to", parameters["to_date"],
            ], 1920)
            if result == 3:
                self._finish(job_id, "auth_required", "auth_required")
                return
            if result != 0:
                self._finish(job_id, "failed", str(payload.get("reason") or f"exit_{result}"))
                return
            if payload.get("status") == "complete":
                self._finish(job_id, "completed", None)
                return
            self._record_campaign_progress(job_id, "window_discovered", payload)
            new_batch = payload.get("batch") or {}
            if new_batch.get("status") == BatchStatus.COMPLETED:
                error = self._validate_campaign_batch(job_id, str(new_batch["batch_key"]))
                if error:
                    self._finish(job_id, "failed", error)
                    return

    def _validate_campaign_batch(self, job_id: int, batch_key: str) -> str | None:
        result, payload = self._run_cli(["batch", "validate", batch_key], 120)
        if result != 0 or not payload.get("reconciled") or not payload.get("source_match"):
            return "batch_reconciliation_failed"
        if audit_database(self.settings):
            return "archive_audit_failed"
        with Session(self.engine) as session:
            current_items = session.scalar(select(func.count()).select_from(OAItem)) or 0
        if not capacity_report(
            self.settings.database_path, self.settings.data_root, current_items + 500,
        ).allowed:
            return "capacity_gate_failed"
        self._record_campaign_progress(job_id, "batch_validated", payload)
        return None

    def _run_cli(self, arguments: list[str], timeout: int) -> tuple[int, dict]:
        return run_cli(arguments, self.config_path, timeout)

    def _record_runner_retry(self, job_id: int, result: int, payload: dict, attempt: int) -> None:
        with Session(self.engine) as session:
            job = session.get(OperationJob, job_id)
            if job is None:
                return
            job.last_error_code = f"runner_retry_{attempt}_exit_{result}"
            self._heartbeat(session, job)
            self._event(session, job, "runner_retry", "running", {
                "attempt": attempt,
                "exit_code": result,
                "reason": payload.get("reason") or payload.get("run_status") or "unknown",
                "stderr_sha256": payload.get("stderr_sha256"),
            })
            session.commit()

    def _record_campaign_progress(self, job_id: int, event_type: str, details: dict) -> None:
        with Session(self.engine) as session:
            job = session.get(OperationJob, job_id)
            if job is None:
                return
            resolved = session.scalar(select(func.count()).select_from(BatchItem).join(CollectionBatch).where(
                CollectionBatch.notes.like("backfill:v1%"),
                BatchItem.archive_status.in_(("archived", "confirmed_skip", "review_required")),
            )) or 0
            job.progress_current = resolved
            job.last_error_code = None
            self._heartbeat(session, job)
            self._event(session, job, event_type, "running", details)
            session.commit()

    @staticmethod
    def _heartbeat(session: Session, job: OperationJob) -> None:
        now = datetime.now(timezone.utc)
        job.heartbeat_at = now
        job.lease_expires_at = now + LEASE_TTL

    @staticmethod
    def _record_collection_issues(session: Session, batch_id: int) -> None:
        failed_items = session.scalars(select(BatchItem).where(
            BatchItem.batch_id == batch_id,
            BatchItem.archive_status.in_(("collect_failed", "download_failed")),
        )).all()
        for item in failed_items:
            exists = session.scalar(select(ReviewEntry.id).where(
                ReviewEntry.kind == "collection_issue",
                ReviewEntry.details_json.contains(f'"batch_item_id": {item.id}'),
                ReviewEntry.details_json.contains(f'"retry_count": {item.retry_count}'),
                ReviewEntry.status == "pending",
            ).limit(1))
            if exists is None:
                session.add(ReviewEntry(
                    kind="collection_issue", item_id=item.oa_item_id,
                    details_json=json.dumps({
                        "batch_id": batch_id, "batch_item_id": item.id,
                        "ordinal": item.ordinal, "title": item.title,
                        "retry_count": item.retry_count,
                        "archive_status": item.archive_status,
                        "error": item.last_error,
                    }, ensure_ascii=False),
                ))

    @staticmethod
    def _resolve_recovered_collection_issues(session: Session, batch_id: int) -> None:
        reviews = session.scalars(select(ReviewEntry).where(
            ReviewEntry.kind == "collection_issue", ReviewEntry.status == "pending",
        )).all()
        for review in reviews:
            try:
                details = json.loads(review.details_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if details.get("batch_id") != batch_id:
                continue
            item = session.get(BatchItem, details.get("batch_item_id"))
            if item is not None and item.archive_status in {"archived", "confirmed_skip"}:
                review.status = "resolved"

    def _finish(self, job_id: int, status: str, error: str | None) -> None:
        with Session(self.engine) as session:
            job = session.get(OperationJob, job_id)
            if job is None:
                return
            job.status = status
            job.last_error_code = error
            job.finished_at = datetime.now(timezone.utc)
            job.lease_owner = None
            job.lease_expires_at = None
            self._event(session, job, "finished", status, {"error": error})
            session.commit()

    def _set_failed(self, session: Session, job: OperationJob, error: str) -> None:
        job.status = "failed"
        job.last_error_code = error
        job.finished_at = datetime.now(timezone.utc)
        job.lease_owner = None
        job.lease_expires_at = None
        self._event(session, job, "finished", "failed", {"error": error})

    @staticmethod
    def _event(session: Session, job: OperationJob, event_type: str, status: str, details: dict | None = None) -> None:
        sequence = (session.scalar(select(func.max(OperationEvent.sequence)).where(OperationEvent.job_id == job.id)) or 0) + 1
        job.events.append(OperationEvent(
            sequence=sequence, event_type=event_type, status=status,
            details_json=json.dumps(details or {}, ensure_ascii=False),
        ))
