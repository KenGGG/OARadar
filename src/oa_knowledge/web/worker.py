from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.archive import atomic_write_bytes
from oa_knowledge.constants import BatchStatus, LEASE_TTL
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import ArchivedFile, BatchItem, CollectionBatch, ExclusionPolicy, ItemOccurrence, ItemSnapshot, MarkdownQueueControl, MarkdownTask, NotificationDelivery, OAItem, OAManifestItem, OnlineAuditRun, OperationEvent, OperationJob, ParseArtifact, ParseJob, PipelineTask, ReviewEntry, SourceAttachment, SummaryVersion
from oa_knowledge.production_pipeline import ProductionQueue
from oa_knowledge.ops.audit import audit_database
from oa_knowledge.source_roles import MARKDOWN_SOURCE_ROLES
from oa_knowledge.ops.capacity import capacity_report
from oa_knowledge.web.cli_runner import run_cli
from oa_knowledge.web.status import _apply_policy_to_pending, execute_archive_job


ONLINE_AUDIT_BATCH_SIZE = 25
ONLINE_AUDIT_BATCH_SECONDS = 60
ONLINE_AUDIT_YIELD = timedelta(seconds=5)
PIPELINE_HEARTBEAT_SECONDS = 60


def _pump_stream(stream, buffer: list[str]) -> None:
    try:
        for line in stream:
            buffer.append(line)
    finally:
        stream.close()


def _has_verified_attachment(session: Session, oa_item_key: str) -> bool:
    """True when the OA item already has at least one verified source attachment."""
    item = session.scalar(select(OAItem.id).where(OAItem.oa_item_key == oa_item_key))
    if item is None:
        return False
    count = session.scalar(select(func.count()).select_from(ArchivedFile).where(
        ArchivedFile.oa_item_id == item,
        ArchivedFile.file_role.in_(MARKDOWN_SOURCE_ROLES),
        ArchivedFile.download_status == "verified",
    )) or 0
    return count > 0


@dataclass(frozen=True)
class ManifestRetrySnapshot:
    """Durable facts for one immutable manifest-retry target list."""

    total: int
    success: int
    failed: int
    pending_keys: tuple[str, ...]

    @property
    def progress_current(self) -> int:
        return self.success + self.failed

    @property
    def complete(self) -> bool:
        return not self.pending_keys and self.failed == 0 and self.progress_current == self.total


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
        (self.settings.runtime_root / "operation-worker.json").unlink(missing_ok=True)

    @staticmethod
    def _retry_progress(total_targets: int, resumed: int, completed_after_resume: int) -> int:
        return min(total_targets, resumed + completed_after_resume)

    def _manifest_retry_snapshot(self, job_id: int) -> ManifestRetrySnapshot:
        """Return retry progress from target facts, never from a list offset."""
        terminal = {"downloaded", "no_attachment", "skipped"}
        failed_statuses = {"download_failed", "auth_required", "depth_limit_reached", "partial"}
        with Session(self.engine) as session:
            job = session.get(OperationJob, job_id)
            if job is None:
                raise LookupError(f"manifest retry job {job_id} is missing")
            parameters = json.loads(job.parameters_json or "{}")
            keys = tuple(parameters.get("oa_item_keys") or ())
            source_status = parameters.get("source_status")
            started_at = job.started_at
            rows = {
                row.oa_item_key: row
                for row in session.scalars(select(OAManifestItem).where(
                    OAManifestItem.oa_item_key.in_(keys),
                ))
            }

        success = 0
        failed = 0
        pending: list[str] = []
        for key in keys:
            row = rows.get(key)
            if row is None:
                failed += 1
                continue
            attempted = bool(started_at and row.last_retry_at and row.last_retry_at >= started_at)
            if row.processing_status in terminal:
                if (
                    row.processing_status == "no_attachment"
                    and source_status == "no_attachment"
                    and not attempted
                ):
                    pending.append(key)
                    continue
                success += 1
                continue
            if attempted and row.processing_status in failed_statuses:
                failed += 1
            else:
                pending.append(key)
        return ManifestRetrySnapshot(len(keys), success, failed, tuple(pending))

    @staticmethod
    def _completed_scan_progress(pending: dict, done: dict) -> tuple[int, int]:
        """Represent a finished scan as 100%; detailed counts remain separate."""
        processed = (pending.get("source_total", 0) or 0) + (done.get("new_items", 0) or 0)
        return processed, processed

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
        # Keep verified migration ahead of historical Markdown work. During an
        # unfinished audit this is a cheap no-op; after completion it creates
        # the durable migration job before history can be claimed.
        from oa_knowledge.archive_migration_campaign import ensure_verified_archive_migration
        ensure_verified_archive_migration(self.engine)
        job_id = self._claim_next()
        if job_id is None:
            queue_names = None
            with Session(self.engine) as session:
                online_audit_waiting = session.scalar(select(OperationJob.id).where(
                    OperationJob.job_type == "online_audit",
                    OperationJob.status.in_(("queued", "running")),
                ).limit(1)) is not None
            if online_audit_waiting:
                # Audit batches deliberately yield for realtime work. Historical
                # rebuilds wait until verification finishes.
                queue_names = ("realtime_pending", "realtime_done")
            task = self.production_queue.claim(self.owner, queue_names=queue_names)
            if task is None:
                return False
            self._write_runtime_status("working", self._pipeline_activity(task.stage))
            self._execute_pipeline_task(task)
            return True
        with Session(self.engine) as session:
            job = session.get(OperationJob, job_id)
            if job is None:
                return False
            job_type = job.job_type
        try:
            self._write_runtime_status("working", self._job_activity(job_type))
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
            elif job_type == "verified_archive_migration":
                self._execute_verified_archive_migration(job_id)
            elif job_type == "archive_date_reconcile":
                self._execute_archive_date_reconcile(job_id)
            elif job_type in ("scheduled_bootstrap", "scheduled_hourly", "scheduled_nightly"):
                self._execute_scheduled_scan(job_id)
            elif job_type == "data_governance":
                self._execute_data_governance(job_id)
            else:
                self._finish(job_id, "failed", "unsupported_job_type")
        except Exception as exc:
            if job_type == "online_audit":
                from oa_knowledge.db.models import OnlineAuditRun
                from oa_knowledge.online_audit import fail_audit
                with Session(self.engine) as session:
                    run_id = session.scalar(select(OnlineAuditRun.id).where(OnlineAuditRun.job_id == job_id))
                if run_id is not None:
                    fail_audit(self.settings, run_id, "AUDIT_WORKER_ERROR")
            self._finish(job_id, "failed", f"worker_exception_{type(exc).__name__}")
        return True

    @staticmethod
    def _job_activity(job_type: str) -> str:
        return {
            "online_audit": "正在逐项核验已办",
            "scheduled_bootstrap": "正在执行初始化扫描",
            "scheduled_hourly": "正在扫描待办与新增已办",
            "scheduled_nightly": "正在执行夜间归档检查",
            "full_manifest": "正在同步 OA 已办清单",
            "full_manifest_retry": "正在重试已办清单同步",
            "done_incremental": "正在增量检查已办",
            "archive_batch": "正在下载并归档已办",
            "backfill_campaign": "正在补齐历史已办",
            "verified_archive_migration": "正在迁移已核验历史原件",
        }.get(job_type, "正在执行本地后台任务")

    @staticmethod
    def _pipeline_activity(stage: str) -> str:
        return {
            "pending_detail": "正在下载待办详情",
            "pending_parse": "正在生成待办 Source Markdown",
            "pending_summary": "正在生成待办摘要",
            "notify_feishu": "正在发送待办飞书提醒",
            "done_capture_and_archive": "正在下载并归档已办",
            "attachment_inventory": "正在核对已办附件清单",
            "parse": "正在生成已办 Source Markdown",
            "source_publish": "正在发布来源 Markdown",
            "curation": "正在整理知识目录",
        }.get(stage, "正在执行 OA 流水线任务")

    def _verify_oa_login(self, browser) -> object:
        self._write_runtime_status("logging_in", "正在验证 OA 登录")
        state = browser.login_with_saved_credentials(30)
        self._write_runtime_status("working", "OA 登录已验证，正在继续当前任务")
        return state

    def _execute_verified_archive_migration(self, job_id: int) -> None:
        """Move one restart-safe batch after online evidence has passed."""
        from oa_knowledge.archive_migration_campaign import (
            eligible_legacy_done_ids,
            migration_review_count,
        )
        from oa_knowledge.archive_reconciliation import migrate_archive_item_by_id

        with Session(self.engine) as session:
            job = session.get(OperationJob, job_id)
            if job is None or job.status not in {"queued", "running"}:
                return
            params = json.loads(job.parameters_json or "{}")
            audit_run_id = int(params["audit_run_id"])
            audit = session.get(OnlineAuditRun, audit_run_id)
            if audit is None or audit.status != "completed":
                job.status = "paused"
                job.last_error_code = "WAITING_FOR_ONLINE_AUDIT"
                job.lease_owner = None
                job.lease_expires_at = None
                self._event(
                    session, job, "verified_archive_migration_waiting",
                    "paused", {"reason": "online_audit_reopened"},
                )
                session.commit()
                return

            control = session.get(MarkdownQueueControl, 1)
            if control is None:
                control = MarkdownQueueControl(id=1, paused=True)
                session.add(control)
                params.setdefault("markdown_was_paused", False)
            else:
                params.setdefault("markdown_was_paused", bool(control.paused))
                control.paused = True
            job.parameters_json = json.dumps(params, sort_keys=True)

            active_markdown = session.scalar(select(func.count()).select_from(MarkdownTask).where(
                MarkdownTask.status == "running",
            )) or 0
            active_pipeline = session.scalar(select(func.count()).select_from(PipelineTask).where(
                PipelineTask.status == "running",
            )) or 0
            if active_markdown or active_pipeline:
                job.status = "queued"
                job.parameters_json = json.dumps(params, sort_keys=True)
                job.heartbeat_at = datetime.now(timezone.utc)
                job.lease_owner = None
                job.lease_expires_at = None
                session.commit()
                return

            job.status = "running"
            job.started_at = job.started_at or datetime.now(timezone.utc)
            failed_ids = {int(value) for value in params.get("failed_item_ids", [])}
            item_ids = eligible_legacy_done_ids(
                session, audit_run_id, exclude_ids=failed_ids, limit=25,
            )
            session.commit()

        migrated = 0
        new_failed_ids: list[int] = []
        for item_id in item_ids:
            try:
                with Session(self.engine) as item_session:
                    migrate_archive_item_by_id(item_session, self.settings, item_id)
                migrated += 1
            except (FileNotFoundError, FileExistsError, OSError, ValueError):
                new_failed_ids.append(item_id)
                with Session(self.engine) as review_session:
                    exists = review_session.scalar(select(ReviewEntry.id).where(
                        ReviewEntry.kind == "archive_path_migration",
                        ReviewEntry.item_id == item_id,
                        ReviewEntry.status == "pending",
                    ).limit(1))
                    if exists is None:
                        review_session.add(ReviewEntry(
                            kind="archive_path_migration",
                            item_id=item_id,
                            details_json=json.dumps({
                                "reason_code": "ARCHIVE_MIGRATION_REVIEW_REQUIRED",
                            }),
                            status="pending",
                        ))
                        review_session.commit()

        with Session(self.engine) as session:
            job = session.get(OperationJob, job_id)
            if job is None:
                return
            params = json.loads(job.parameters_json or "{}")
            failed_ids = {int(value) for value in params.get("failed_item_ids", [])}
            failed_ids.update(new_failed_ids)
            params["failed_item_ids"] = sorted(failed_ids)
            params["processed"] = int(params.get("processed", 0)) + len(item_ids)
            params["migrated"] = int(params.get("migrated", 0)) + migrated
            params["failed"] = len(failed_ids)
            remaining = eligible_legacy_done_ids(
                session, int(params["audit_run_id"]), exclude_ids=failed_ids, limit=1,
            )
            job.progress_current = int(params["processed"])
            job.parameters_json = json.dumps(params, sort_keys=True)
            now = datetime.now(timezone.utc)
            job.heartbeat_at = now
            if remaining:
                job.status = "queued"
                job.lease_owner = None
                job.lease_expires_at = None
                session.commit()
                return

            params["review_required"] = migration_review_count(
                session, int(params["audit_run_id"]),
            ) + len(failed_ids)
            params["historical_released_tasks"] = (
                self.production_queue.release_verified_historical_tasks(
                    int(params["audit_run_id"]), session=session,
                )
            )
            params["historical_review_tasks"] = (
                self.production_queue.finalize_ineligible_historical_tasks(
                    int(params["audit_run_id"]), session=session,
                )
            )
            job.parameters_json = json.dumps(params, sort_keys=True)
            control = session.get(MarkdownQueueControl, 1)
            if control is not None:
                control.paused = bool(params.get("markdown_was_paused", False))
            job.status = "completed"
            job.finished_at = now
            job.last_error_code = None
            job.lease_owner = None
            job.lease_expires_at = None
            self._event(session, job, "verified_archive_migration_finished", "completed", {
                "migrated": params["migrated"],
                "failed": params["failed"],
                "review_required": params["review_required"],
                "historical_released_tasks": params["historical_released_tasks"],
                "historical_review_tasks": params["historical_review_tasks"],
            })
            session.commit()

    def _execute_data_governance(self, job_id: int) -> None:
        """Execute one filesystem-heavy governance action outside HTTP requests."""
        from oa_knowledge.data_governance.quarantine import purge_run, quarantine_run, restore_run
        from oa_knowledge.data_governance.service import build_cleanup_plan
        from oa_knowledge.integrity_reconciliation import classify_integrity_issues, persist_integrity_summary

        with Session(self.engine) as session:
            job = session.get(OperationJob, job_id)
            if job is None or job.status not in {"queued", "running"}:
                return
            parameters = json.loads(job.parameters_json)
            job.status = "running"
            job.started_at = job.started_at or datetime.now(timezone.utc)
            session.commit()
        action = parameters["action"]
        if action == "integrity_audit":
            result = classify_integrity_issues(self.settings, self.engine)
            summary = {
                "total": result.total,
                "issue_counts": result.issue_counts,
                "reason_counts": result.reason_counts,
            }
            persist_integrity_summary(
                self.engine, result, run_key=f"integrity-reconciliation:{job_id}",
            )
        elif action == "plan":
            result = build_cleanup_plan(
                self.settings, self.engine, categories=set(parameters["categories"]),
            )
            summary = {
                "action": action, "run_id": result.run_id,
                "candidate_count": result.candidate_count,
                "candidate_bytes": result.candidate_bytes,
            }
        else:
            run_id = int(parameters["run_id"])
            if action == "quarantine":
                result = quarantine_run(self.settings, self.engine, run_id)
            elif action == "restore":
                result = restore_run(self.settings, self.engine, run_id)
            elif action == "purge":
                result = purge_run(
                    self.settings, self.engine, run_id,
                    confirmation=str(parameters.get("confirmation", "")),
                )
            else:
                raise ValueError("unsupported data-governance action")
            summary = {
                "action": action, "run_id": run_id,
                "succeeded_count": result.succeeded_count,
                "skipped_count": result.skipped_count,
                "failed_count": result.failed_count,
                "processed_bytes": result.processed_bytes,
            }
        with Session(self.engine) as session:
            job = session.get(OperationJob, job_id)
            if job is not None:
                job.parameters_json = json.dumps(summary, sort_keys=True)
                session.commit()
        self._finish(job_id, "completed", None)

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

    def _execute_scheduled_scan(self, job_id: int) -> None:
        """Run a scheduled Pending/Done scan as a durable worker job (plan-0806-1 §2.3).

        Delegates to the shared orchestration in ``oa_knowledge.scheduled_sync``
        (the same code the ``oa schedule`` CLI uses) so the browser/lease logic
        lives in exactly one place. The orchestration records a ``Run`` row and
        the worker maps the result into the job's progress/counts.
        """
        from oa_knowledge.db.engine import create_db_engine
        from oa_knowledge.scheduled_sync import (
            run_bootstrap_scan,
            run_hourly_scan,
            run_nightly_scan,
        )

        with Session(self.engine) as session:
            job = session.get(OperationJob, job_id)
            if job is None or job.status not in {"queued", "running"}:
                return
            job_type = job.job_type
            job.status = "running"
            job.started_at = job.started_at or datetime.now(timezone.utc)
            job.heartbeat_at = datetime.now(timezone.utc)
            job.lease_expires_at = job.heartbeat_at + LEASE_TTL
            self._event(session, job, "scheduled_scan_started", "running", {"job_type": job_type})
            session.commit()

        scan_func = {
            "scheduled_bootstrap": run_bootstrap_scan,
            "scheduled_hourly": run_hourly_scan,
            "scheduled_nightly": run_nightly_scan,
        }[job_type]

        # Use a throwaway engine so the orchestration's engine.dispose() does not
        # tear down the worker's shared engine.
        scan_engine = create_db_engine(self.settings.database_path)
        try:
            try:
                result = scan_func(scan_engine, self.settings, headed=False)
            except RuntimeError as exc:
                message = str(exc)
                if "authentication" in message.lower():
                    self._finish_scheduled(job_id, "auth_required", "OA_AUTH_EXPIRED", {})
                else:
                    self._finish_scheduled(job_id, "failed", "SCAN_FAILED", {})
                return
            except Exception as exc:
                self._finish_scheduled(job_id, "failed", f"scheduled_exception_{type(exc).__name__}", {})
                return

            pending = result.get("pending", {})
            done = result.get("done", {})
            raw_status = result.get("status", "completed")
            if raw_status == "partial":
                final_status, error = "partial", None
            elif raw_status == "failed":
                final_status, error = "failed", "SCAN_FAILED"
            else:
                final_status, error = "completed", None

            counts = {
                "pending_scanned": pending.get("source_total", 0),
                "pending_created": pending.get("created", 0),
                "pending_updated": pending.get("updated", 0),
                "done_new": done.get("new_items", 0),
                "done_archive_jobs": done.get("download_jobs_enqueued", 0),
                "knowledge_tasks": done.get(
                    "knowledge_tasks_enqueued", done.get("markdown_tasks_enqueued", 0),
                ),
                "feishu_notify_tasks": pending.get("tasks_enqueued", pending.get("created", 0) + pending.get("updated", 0)),
            }
            progress_current, progress_total = self._completed_scan_progress(pending, done)
            self._finish_scheduled(job_id, final_status, error, counts, progress_current, progress_total)
        finally:
            scan_engine.dispose()

    def _finish_scheduled(
        self, job_id: int, status: str, error: str | None, counts: dict,
        progress_current: int = 0, progress_total: int | None = None,
    ) -> None:
        with Session(self.engine) as session:
            job = session.get(OperationJob, job_id)
            if job is None:
                return
            job.status = status
            job.last_error_code = error
            job.finished_at = datetime.now(timezone.utc)
            job.progress_current = progress_current
            job.progress_total = progress_total
            job.lease_owner = None
            job.lease_expires_at = None
            self._event(session, job, "scheduled_scan_finished", status, {
                "error": error,
                "counts": counts,
            })
            session.commit()

    def _execute_online_audit(self, job_id: int) -> None:
        from oa_knowledge.collector.browser import BrowserSession, LoginState
        from oa_knowledge.collector.detail import CollaborationDetailAdapter
        from oa_knowledge.db.models import OnlineAuditRun
        from oa_knowledge.online_audit import ATTACHMENT_ROLES, AuditObservation, execute_audit, fail_audit, fingerprint_attachments
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
            if self._verify_oa_login(browser) != LoginState.AUTHENTICATED:
                fail_audit(self.settings, run_id, "OA_AUTH_EXPIRED")
                self._finish(job_id, "failed", "OA_AUTH_EXPIRED")
                return
            if browser.page is None:
                raise RuntimeError("browser page is not available")
            # Strict verification deliberately downloads fresh OA bytes instead
            # of resolving from the local archive cache. The capture remains in
            # memory and never overwrites an existing original.
            verification_adapter = CollaborationDetailAdapter(browser.page)

            def inspect(item) -> AuditObservation:
                if not item.workitem_id_text:
                    raise RuntimeError("OA item identifier unavailable")
                capture = verification_adapter.capture_direct(
                    browser.base_url, item.workitem_id_text,
                    max_depth=self.settings.collector.max_attachment_depth,
                    total_timeout_seconds=self.settings.online_audit.item_timeout_seconds,
                    download_timeout_seconds=self.settings.online_audit.download_timeout_seconds,
                )
                if any(
                    issue.get("kind") == "capture_timeout"
                    for issue in capture.capture_issues
                ):
                    raise TimeoutError("online audit item capture timed out")
                attachments = list(capture.attachments)
                for container in capture.related_containers:
                    attachments.extend(container.attachments)
                by_key = {}
                for attachment in attachments:
                    if attachment.file_role not in ATTACHMENT_ROLES:
                        continue
                    digest = hashlib.sha256(attachment.content).hexdigest() if attachment.content is not None else None
                    by_key.setdefault(attachment.attachment_key, (
                        attachment.file_role, attachment.attachment_key,
                        attachment.size_bytes, digest,
                    ))
                inventory_hash, content_hash = fingerprint_attachments(list(by_key.values()))
                depth_limit = any(container.has_unvisited_children for container in capture.related_containers)
                depth_limit = depth_limit or any(
                    issue.get("kind") == "depth_limit_reached" for issue in capture.capture_issues
                )
                return AuditObservation(
                    recognized_attachments=len(by_key),
                    online_inventory_sha256=inventory_hash,
                    online_content_sha256=content_hash,
                    depth_limit_reached=depth_limit,
                    attachment_evidence=tuple(by_key.values()),
                )

            try:
                execute_audit(
                    self.settings, run_id,
                    inspect_item=inspect,
                    max_items=ONLINE_AUDIT_BATCH_SIZE,
                    max_seconds=ONLINE_AUDIT_BATCH_SECONDS,
                )
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
        from oa_knowledge.production_pipeline import CORE_PIPELINE_STAGES

        if task.stage not in CORE_PIPELINE_STAGES:
            self.production_queue.fail(
                task.id, self.owner, "RETIRED_STAGE", task.stage, recoverable=False,
            )
            return

        heartbeat_stop = threading.Event()

        def refresh_lease() -> None:
            while not heartbeat_stop.wait(PIPELINE_HEARTBEAT_SECONDS):
                try:
                    if not self.production_queue.heartbeat(task.id, self.owner):
                        return
                except Exception:
                    # The business task remains authoritative; a transient
                    # SQLite contention will be retried on the next heartbeat.
                    continue

        self.production_queue.heartbeat(task.id, self.owner)
        heartbeat_thread = threading.Thread(target=refresh_lease, daemon=True)
        heartbeat_thread.start()
        try:
            try:
                if task.stage == "attachment_inventory":
                    self._pipeline_attachment_inventory(task)
                elif task.stage == "parse":
                    self._pipeline_parse(task)
                elif task.stage == "detail_sync" and task.queue_name == "realtime_pending":
                    self._pipeline_pending_detail(task)
                elif task.stage == "pending_summary":
                    self._pipeline_pending_summary(task)
                elif task.stage == "notify_feishu":
                    self._pipeline_notify_feishu(task)
                elif task.stage == "pending_cleanup" and task.queue_name == "realtime_pending":
                    self._pipeline_pending_cleanup(task)
                elif task.stage == "oa_resync" and task.queue_name == "realtime_pending":
                    self._pipeline_oa_resync(task)
                elif task.stage == "done_capture_and_archive" and task.queue_name == "realtime_done":
                    self._pipeline_done_capture_and_archive(task)
                elif task.stage == "archive_verify" and task.queue_name == "realtime_done":
                    self._pipeline_archive_verify(task)
                elif task.stage == "pending_parse":
                    self._pipeline_pending_parse(task)
                elif task.stage == "source_publish":
                    self._pipeline_source_publish(task)
                elif task.stage == "classify":
                    self._pipeline_classify(task)
                elif task.stage == "index_publish":
                    self._pipeline_index_publish(task)
                elif task.stage == "curation":
                    self._pipeline_curation(task)
                elif task.stage == "ollama_extract":
                    # Compatibility for tasks created before the unified pipeline.
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
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1)

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
            self.production_queue.advance(task.id, self.owner, "classify")
            return
        self.production_queue.advance(task.id, self.owner, "parse", progress_current=0, progress_total=enqueued)

    @staticmethod
    def _historical_source_files(files):
        return [
            file for file in files
            if file.file_role in MARKDOWN_SOURCE_ROLES
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
        self.production_queue.advance(task.id, self.owner, "source_publish", progress_current=completed, progress_total=len(jobs))

    def _pipeline_source_publish(self, task: PipelineTask) -> None:
        from oa_knowledge.parsers.eligibility import evaluate_eligibility
        from oa_knowledge.source_markdown.service import publish_active_artifact
        from oa_knowledge.storage_paths import resolve_data_path

        with Session(self.engine) as session:
            item = session.scalar(select(OAItem).where(OAItem.oa_item_key == task.logical_item_key))
            if item is None:
                self.production_queue.fail(
                    task.id, self.owner, "ARCHIVED_ITEM_MISSING", "archived item is missing", recoverable=False,
                )
                return
            files = self._historical_source_files(session.scalars(select(ArchivedFile).where(
                ArchivedFile.oa_item_id == item.id,
                ArchivedFile.download_status == "verified",
                ArchivedFile.local_relpath.is_not(None),
            ).order_by(ArchivedFile.id)).all())
            publishable_files = []
            retry_required = False
            for source in files:
                try:
                    source_path = resolve_data_path(
                        self.settings.data_root,
                        source.local_relpath,
                        allowed_prefixes=("originals",),
                    )
                except ValueError:
                    self.production_queue.fail(
                        task.id, self.owner, "UNSAFE_SOURCE_PATH", "source path is unsafe", recoverable=False,
                    )
                    return
                valid_artifact = session.scalar(select(ParseArtifact.id).where(
                    ParseArtifact.content_object_id == source.content_object_id,
                    ParseArtifact.lifecycle_status == "valid",
                ).limit(1)) if source.content_object_id is not None else None
                if valid_artifact is not None:
                    publishable_files.append(source)
                    continue
                skipped = session.scalar(select(ParseJob.id).where(
                    ParseJob.file_id == source.id,
                    ParseJob.status == "skipped",
                ).limit(1)) is not None
                rejected = session.scalar(
                    select(ParseArtifact.id)
                    .join(ParseJob, ParseArtifact.parse_job_id == ParseJob.id)
                    .where(
                        ParseJob.file_id == source.id,
                        ParseArtifact.lifecycle_status == "rejected",
                    )
                    .limit(1)
                ) is not None
                eligibility = evaluate_eligibility(source_path)
                if skipped or (
                    not eligibility.eligible
                    and eligibility.routing_hint == "review"
                ):
                    # Unsupported is a terminal per-file conversion outcome;
                    # it is rendered in the item index, not sent to Review.
                    continue
                if rejected:
                    self.production_queue.fail(
                        task.id, self.owner, "PARSE_QUALITY_REJECTED", "parse artifact rejected",
                        recoverable=False,
                    )
                    return
                retry_required = True
            if retry_required:
                self.production_queue.advance(
                    task.id, self.owner, "attachment_inventory",
                    progress_current=0, progress_total=len(files),
                )
                return
            # Classification chooses the final directory.  Do not publish a
            # transient unclassified copy and move it later.
        self.production_queue.advance(
            task.id, self.owner, "classify", progress_current=0, progress_total=len(files),
        )

    def _pipeline_classify(self, task: PipelineTask) -> None:
        from oa_knowledge.markdown_delivery import classify_done_item

        with Session(self.engine) as session:
            classify_done_item(session, task.logical_item_key)
            item = session.scalar(select(OAItem).where(OAItem.oa_item_key == task.logical_item_key))
            files = [] if item is None else [
                source for source in self._historical_source_files(session.scalars(select(ArchivedFile).where(
                    ArchivedFile.oa_item_id == item.id,
                    ArchivedFile.download_status == "verified",
                    ArchivedFile.local_relpath.is_not(None),
                ).order_by(ArchivedFile.id)).all())
                if source.content_object_id is not None and session.scalar(select(ParseArtifact.id).where(
                    ParseArtifact.content_object_id == source.content_object_id,
                    ParseArtifact.lifecycle_status == "valid",
                ).limit(1)) is not None
            ]
            try:
                for source in files:
                    publish_active_artifact(session, self.settings, source.id)
                session.commit()
            except (FileNotFoundError, ValueError):
                session.rollback()
                self.production_queue.advance(task.id, self.owner, "attachment_inventory")
                return
        self.production_queue.advance(task.id, self.owner, "index_publish")

    def _pipeline_index_publish(self, task: PipelineTask) -> None:
        from oa_knowledge.markdown_delivery import publish_item_index

        with Session(self.engine) as session:
            publish_item_index(session, self.settings, task.logical_item_key)
            session.commit()
        self.production_queue.complete(task.id, self.owner)

    def _pipeline_curation(self, task: PipelineTask) -> None:
        from oa_knowledge.curation.service import run_curation

        result = run_curation(
            self.settings, self.engine, limit=1, oa_item_key=task.logical_item_key,
        )
        if result.failed:
            self.production_queue.fail(
                task.id, self.owner, "CURATION_FAILED", "local curation failed", recoverable=True,
            )
            return
        self.production_queue.complete(task.id, self.owner)

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
                if self._verify_oa_login(browser) != LoginState.AUTHENTICATED:
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
        # Notification delivery is decoupled from the OA capture pipeline. The
        # notify flag decides whether the summary advances into the delivery
        # stage (notify=True) or is considered complete as-is (notify=False,
        # e.g. the first-deploy baseline). A requested notification must never
        # advance into an unsupported stage.
        payload = json.loads(task.payload_json or "{}")
        if payload.get("notify", True):
            self.production_queue.advance(task.id, self.owner, "notify_feishu")
        else:
            self.production_queue.complete(task.id, self.owner)

    def _pipeline_notify_feishu(self, task: PipelineTask) -> None:
        from oa_knowledge.collector.pending import PendingAdapter
        from oa_knowledge.config import validate_feishu_runtime_config
        from oa_knowledge.notifications.feishu_service import FeishuService, apply_delivery_result
        from oa_knowledge.pending_summary import PendingSummary

        logical_item_id = int(task.logical_item_key)
        payload = json.loads(task.payload_json or "{}")

        # Honor feishu.enabled and fail loudly on misconfiguration. Never treat
        # a broken config as a successful send (plan-0805-02 §1.1).
        state = validate_feishu_runtime_config(self.settings)
        if state == "disabled":
            self.production_queue.complete(task.id, self.owner)
            return
        if state != "ready":
            safe_error = {
                "missing_webhook": "feishu webhook not configured",
                "missing_secret": "feishu signing secret not configured",
                "invalid_webhook": "feishu webhook URL is invalid",
            }.get(state, "feishu misconfigured")
            self.production_queue.fail(
                task.id, self.owner, "FEISHU_MISCONFIGURED", safe_error, recoverable=False
            )
            return

        service = FeishuService(self.settings)

        with Session(self.engine) as session:
            version = session.scalar(
                select(SummaryVersion).where(
                    SummaryVersion.logical_item_id == logical_item_id,
                    SummaryVersion.summary_kind == "pending",
                    SummaryVersion.status == "current",
                ).order_by(SummaryVersion.version.desc()).limit(1)
            )
            if version is None:
                self.production_queue.fail(task.id, self.owner, "PENDING_SUMMARY_MISSING", "no current pending summary", recoverable=False)
                return
            summary = PendingSummary.model_validate_json(version.structured_json)

            occurrence = None
            occurrence_id = payload.get("occurrence_id")
            if occurrence_id is not None:
                occurrence = session.get(ItemOccurrence, int(occurrence_id))
            if occurrence is None:
                occurrence = session.scalar(select(ItemOccurrence).where(
                    ItemOccurrence.logical_item_id == logical_item_id,
                    ItemOccurrence.channel == "pending",
                ).order_by(ItemOccurrence.id.desc()).limit(1))
            title = (occurrence.title if occurrence else None) or summary.matter_type or f"待办 {logical_item_id}"
            sender = occurrence.sender if occurrence else None
            current_node = occurrence.current_node if occurrence else None
            deadline_text = occurrence.deadline_text if occurrence else None
            detail_url = occurrence.detail_url if (occurrence and occurrence.detail_url) else None
            if not detail_url and occurrence and occurrence.affair_id_text:
                detail_url = PendingAdapter.detail_url(self.settings.browser.base_url, occurrence.affair_id_text)

            idem_key = f"feishu:pending:{logical_item_id}:{version.input_hash}"
            existing = session.scalar(select(NotificationDelivery).where(
                NotificationDelivery.idempotency_key == idem_key,
            ))
            if existing is not None and existing.status == "sent":
                session.expunge(existing)
                self.production_queue.advance(task.id, self.owner, "pending_cleanup")
                return
            delivery = existing or NotificationDelivery(
                logical_item_id=logical_item_id,
                snapshot_id=version.snapshot_id,
                channel="feishu",
                notification_type="pending_summary",
                idempotency_key=idem_key,
                status="queued",
            )
            delivery.channel = "feishu"
            delivery.notification_type = "pending_summary"
            delivery.attempts = (delivery.attempts or 0) + 1
            delivery.status = "sending"
            session.add(delivery)
            session.commit()

        result = service.send_pending_summary(
            summary,
            title=title,
            sender=sender or "",
            current_node=current_node or "",
            deadline_text=deadline_text or "",
            detail_url=detail_url or "",
        )
        now = datetime.now(timezone.utc)
        with Session(self.engine) as session:
            delivery = session.scalar(select(NotificationDelivery).where(
                NotificationDelivery.idempotency_key == idem_key,
            ))
            if delivery is None:
                delivery = NotificationDelivery(
                    logical_item_id=logical_item_id,
                    snapshot_id=version.snapshot_id,
                    channel="feishu",
                    notification_type="pending_summary",
                    idempotency_key=idem_key,
                )
                session.add(delivery)
            apply_delivery_result(delivery, result, now)
            session.commit()

        # A successful send advances to the independent cleanup stage. A
        # retryable transport error is re-queued (with backoff) by the queue;
        # anything else is parked for manual retry so we never blindly re-push
        # an uncertain delivery (§3.2).
        if result.status == "sent":
            self.production_queue.advance(task.id, self.owner, "pending_cleanup")
        else:
            self.production_queue.fail(
                task.id, self.owner, "FEISHU_SEND_FAILED",
                result.safe_error or "feishu delivery failed", recoverable=result.retryable,
            )

    def _pipeline_pending_cleanup(self, task: PipelineTask) -> None:
        from oa_knowledge.pending_cleanup import CLEANED, perform_cleanup

        payload = json.loads(task.payload_json or "{}")
        occurrence_id = payload.get("occurrence_id")
        with Session(self.engine) as session:
            occurrence = session.get(ItemOccurrence, occurrence_id) if occurrence_id is not None else session.scalar(
                select(ItemOccurrence).where(
                    ItemOccurrence.logical_item_id == int(task.logical_item_key),
                    ItemOccurrence.channel == "pending",
                ).order_by(ItemOccurrence.id.desc()).limit(1)
            )
            if occurrence is None:
                self.production_queue.fail(task.id, self.owner, "PENDING_OCCURRENCE_MISSING", "pending occurrence missing", recoverable=False)
                return
            if occurrence.cleanup_status == CLEANED:
                self.production_queue.complete(task.id, self.owner)
                return
            try:
                perform_cleanup(
                    session, occurrence, self.settings, datetime.now(timezone.utc),
                    retry_failed=True,
                )
                session.commit()
            except ValueError as exc:
                session.rollback()
                self.production_queue.fail(task.id, self.owner, "PENDING_CLEANUP_NOT_READY", str(exc), recoverable=True)
                return
            except Exception as exc:  # cleanup stores its failure status for retry
                # ``perform_cleanup`` records ``cleanup_failed`` before it
                # raises. Persist that ledger state; rolling back here would
                # turn a retry into an opaque repeated failure.
                session.commit()
                self.production_queue.fail(task.id, self.owner, "PENDING_CLEANUP_FAILED", type(exc).__name__, recoverable=True)
                return
        self.production_queue.complete(task.id, self.owner)

    def _pipeline_oa_resync(self, task: PipelineTask) -> None:
        """Re-sync a single cleaned Pending item's display columns from OA.

        Triggered by the web console "与 OA 同步" action. Refreshes only the
        title/sender/current_node/deadline of the targeted occurrence (no
        re-capture, summary, or Feishu re-notify, no business-body persistence);
        if the item is no longer in OA it marks ``oa_gone_at`` (plan-0807-1 §sync).
        """
        from oa_knowledge.pending_sync import resync_pending_item_from_oa
        from oa_knowledge.resources import ResourceCoordinator

        payload = json.loads(task.payload_json or "{}")
        occurrence_id = payload.get("occurrence_id")
        if occurrence_id is None:
            self.production_queue.fail(task.id, self.owner, "OA_RESYNC_NO_OCCURRENCE", "missing occurrence_id", recoverable=False)
            return
        coordinator = ResourceCoordinator(self.engine)
        lease = coordinator.acquire("oa_browser", f"{self.owner}:oa-resync:{task.id}", ttl_seconds=600, uses_local_gpu=False)
        if lease is None:
            raise RuntimeError("OA browser is busy")
        try:
            found = resync_pending_item_from_oa(self.settings, self.engine, int(occurrence_id))
        finally:
            coordinator.release(lease, f"{self.owner}:oa-resync:{task.id}")
        self.production_queue.complete(task.id, self.owner)

    def _pipeline_done_capture_and_archive(self, task: PipelineTask) -> None:
        """Capture + archive a newly discovered Done item, then hand off (plan-0806-1 §3).

        Reuses the existing read-only archive stack (``CollaborationDetailAdapter``,
        ``archive_collaboration_detail``, ``archive_proxy``, ``verified_attachment_resolver``)
        instead of reimplementing archiving. The item is captured, attachments are
        downloaded and verified, the ``OAItem``/``OAManifestItem`` are updated, verified
        files are queued for Markdown export, and the task advances to
        ``attachment_inventory`` for parsing/knowledge extraction.
        """
        from oa_knowledge.cli import _oa_detail_url, verified_attachment_resolver
        from oa_knowledge.collector.browser import BrowserSession, LoginState
        from oa_knowledge.collector.detail import AuthRequiredError, CollaborationDetailAdapter
        from oa_knowledge.collector.done import DoneAdapter
        from oa_knowledge.detail_archive import archive_collaboration_detail
        from oa_knowledge.full_manifest import archive_proxy
        from oa_knowledge.resources import ResourceCoordinator

        oa_item_key = task.logical_item_key
        with Session(self.engine) as session:
            manifest = session.scalar(select(OAManifestItem).where(OAManifestItem.oa_item_key == oa_item_key))
            if manifest is None:
                self.production_queue.fail(task.id, self.owner, "MANIFEST_MISSING", "manifest item disappeared", recoverable=False)
                return
            workitem_id = manifest.workitem_id_text
            manifest_id = manifest.id
            manifest_page = manifest.list_page
            manifest_title = manifest.title
        if not workitem_id:
            self.production_queue.fail(task.id, self.owner, "WORKITEM_ID_MISSING", "no workitem id on manifest", recoverable=False)
            return

        coordinator = ResourceCoordinator(self.engine)
        lease_owner = f"{self.owner}:done:{task.id}"
        lease = coordinator.acquire("oa_browser", lease_owner, ttl_seconds=600, uses_local_gpu=False)
        if lease is None:
            raise RuntimeError("OA browser is busy")
        try:
            with BrowserSession(self.settings, headed=False) as browser:
                if self._verify_oa_login(browser) != LoginState.AUTHENTICATED:
                    self.production_queue.fail(task.id, self.owner, "OA_AUTH_EXPIRED", "saved OA session is not authenticated", recoverable=True)
                    return
                if browser.page is None:
                    raise RuntimeError("browser page is not available")
                detail_adapter = CollaborationDetailAdapter(
                    browser.page,
                    attachment_resolver=verified_attachment_resolver(self.engine, self.settings.data_root),
                )
                try:
                    capture = detail_adapter.capture_direct(
                        browser.base_url, workitem_id, max_depth=self.settings.collector.max_attachment_depth,
                        total_timeout_seconds=self.settings.collector.attachment_total_timeout_seconds,
                        download_timeout_seconds=self.settings.collector.download_timeout_seconds,
                    )
                except AuthRequiredError:
                    self.production_queue.fail(task.id, self.owner, "OA_AUTH_EXPIRED", "auth required during detail capture", recoverable=True)
                    return
                except Exception as direct_exc:
                    try:
                        done_adapter = DoneAdapter(
                            browser.page,
                            f"{browser.base_url}{self.settings.browser.done_list_path}",
                        )
                        done_adapter.open_list()
                        located_id = done_adapter.locate_item(
                            manifest_page, manifest_title, workitem_id,
                            self.settings.collector.list_page_delay_seconds,
                        )
                        detail_link = done_adapter.detail_link_for_item(located_id)
                        capture = detail_adapter.capture(
                            located_id, max_depth=self.settings.collector.max_attachment_depth,
                            total_timeout_seconds=self.settings.collector.attachment_total_timeout_seconds,
                            download_timeout_seconds=self.settings.collector.download_timeout_seconds,
                            direct_url=_oa_detail_url(browser.base_url, detail_link) if detail_link else None,
                        )
                    except AuthRequiredError:
                        self.production_queue.fail(task.id, self.owner, "OA_AUTH_EXPIRED", "auth required during detail fallback", recoverable=True)
                        return
                    except Exception as fallback_exc:
                        raise RuntimeError(
                            f"direct detail failed: {type(direct_exc).__name__}; "
                            f"list fallback failed: {type(fallback_exc).__name__}"
                        ) from fallback_exc
                attachments = list(capture.attachments) + [a for container in capture.related_containers for a in container.attachments]
                with Session(self.engine) as session:
                    manifest = session.get(OAManifestItem, manifest_id)
                    assert manifest is not None
                    proxy = archive_proxy(manifest)
                    archive_collaboration_detail(session, proxy, capture, self.settings.data_root)
                    manifest.archive_relpath = session.scalar(
                        select(OAItem.archive_relpath).where(OAItem.oa_item_key == oa_item_key)
                    )
                    if proxy.archive_status == "archived":
                        if attachments or _has_verified_attachment(session, oa_item_key):
                            manifest.processing_status = "downloaded"
                            manifest.no_attachment_confirmed = False
                            manifest.last_error = None
                            manifest.failure_stage = None
                        elif capture.no_attachment_confirmed:
                            manifest.processing_status = "no_attachment"
                            manifest.no_attachment_confirmed = True
                            manifest.last_error = None
                            manifest.failure_stage = None
                        else:
                            manifest.processing_status = "download_failed"
                            manifest.no_attachment_confirmed = False
                            manifest.retry_count += 1
                            manifest.last_error = "OA attachment inventory was not confirmed"
                            manifest.failure_stage = "attachment_inventory"
                    elif proxy.archive_status == "depth_limit_reached":
                        manifest.processing_status = "depth_limit_reached"
                        manifest.no_attachment_confirmed = False
                        manifest.last_error = "attachment container depth limit reached"
                        manifest.failure_stage = "archive_verify"
                    else:
                        manifest.processing_status = "download_failed"
                        manifest.no_attachment_confirmed = False
                        manifest.retry_count += 1
                        manifest.last_error = proxy.last_error
                        manifest.failure_stage = "attachment"
                    verified_sources = session.scalar(select(func.count()).select_from(ArchivedFile).join(OAItem).where(
                        OAItem.oa_item_key == oa_item_key,
                        ArchivedFile.download_status == "verified",
                        ArchivedFile.file_role.in_(MARKDOWN_SOURCE_ROLES),
                    )) or 0
                    session.commit()
                if proxy.archive_status != "archived":
                    code = "DEPTH_LIMIT_REACHED" if proxy.archive_status == "depth_limit_reached" else "ARCHIVE_FAILED"
                    self.production_queue.fail(
                        task.id, self.owner, code, proxy.last_error or "archive failed",
                        recoverable=code != "DEPTH_LIMIT_REACHED",
                    )
                    return
                self.production_queue.advance(
                    task.id, self.owner, "archive_verify",
                    progress_current=verified_sources, progress_total=len(attachments) if attachments else 0,
                )
        finally:
            coordinator.release(lease, lease_owner)

    def _pipeline_archive_verify(self, task: PipelineTask) -> None:
        from oa_knowledge.done_archive import verify_done_archive

        with Session(self.engine) as session:
            result = verify_done_archive(session, self.settings, task.logical_item_key)
            manifest = session.scalar(select(OAManifestItem).where(
                OAManifestItem.oa_item_key == task.logical_item_key,
            ))
            if result.status == "failed":
                if manifest is not None:
                    manifest.processing_status = "download_failed"
                    manifest.failure_stage = "archive_verify"
                    manifest.last_error = result.reason
                    session.commit()
                self.production_queue.fail(
                    task.id, self.owner, result.reason or "ARCHIVE_VERIFY_FAILED",
                    "local archive verification failed", recoverable=False,
                )
                return
            if manifest is not None:
                manifest.processing_status = "downloaded" if result.status == "verified" else "no_attachment"
                manifest.failure_stage = None
                manifest.last_error = None
                session.commit()

        markdown_key = f"markdown:{task.logical_item_key}:{result.content_signature}:v1"
        self.production_queue.enqueue(
            "markdown_delivery", task.logical_item_key, "attachment_inventory", markdown_key,
            payload={"archive_content_signature": result.content_signature},
        )
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
        command = [sys.executable, "-m", "oa_knowledge.cli", "manifest", "run"]
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
            # The first start is an immutable attempt boundary. A numeric
            # progress value cannot safely identify a position in the key list.
            job.started_at = job.started_at or datetime.now(timezone.utc)
            job.progress_current = 0
            job.progress_total = total_targets
            job.heartbeat_at = datetime.now(timezone.utc)
            job.lease_expires_at = job.heartbeat_at + LEASE_TTL
            self._event(session, job, "manifest_retry_started", "running", {"target_count": len(keys)})
            session.commit()
        if not keys:
            self._finish(job_id, "completed")
            return
        last_error: str | None = None
        stderr = ""
        browser_restarts = 0
        while True:
            snapshot = self._manifest_retry_snapshot(job_id)
            with Session(self.engine) as session:
                job = session.get(OperationJob, job_id)
                if job is None:
                    return
                job.progress_current = snapshot.progress_current
                job.progress_total = snapshot.total
                job.heartbeat_at = datetime.now(timezone.utc)
                job.lease_expires_at = job.heartbeat_at + LEASE_TTL
                session.commit()
            if not snapshot.pending_keys:
                break
            chunk = snapshot.pending_keys[:100]
            command = [sys.executable, "-m", "oa_knowledge.cli", "manifest", "download", "--max-items", str(len(chunk))]
            # Target selection is always explicit.  In particular, audit_all
            # must audit the immutable retry target list, not every manifest row.
            command.extend(("--item-ids", ",".join(chunk)))
            if source_status == "download_failed":
                command.append("--failed-only")
            elif source_status == "no_attachment":
                command.append("--recheck-no-attachment")
            elif source_status == "audit_all":
                command.append("--audit-all")
            if self.config_path is not None:
                command.extend(("--config", str(self.config_path)))

            def heartbeat() -> None:
                snapshot = self._manifest_retry_snapshot(job_id)
                with Session(self.engine) as session:
                    job = session.get(OperationJob, job_id)
                    if job is None:
                        return
                    job.progress_current = snapshot.progress_current
                    job.progress_total = snapshot.total
                    job.heartbeat_at = datetime.now(timezone.utc)
                    job.lease_expires_at = job.heartbeat_at + LEASE_TTL
                    active = session.scalar(select(OAManifestItem).where(
                        OAManifestItem.oa_item_key.in_(chunk),
                        OAManifestItem.processing_status == "processing",
                    ).order_by(OAManifestItem.last_retry_at.desc(), OAManifestItem.id.desc()).limit(1))
                    if active:
                        self._event(session, job, "manifest_retry_item_started", "running", {"oa_item_key": active.oa_item_key, "manifest_id": active.id, "item_id": active.workitem_id_text or active.oa_item_key, "title": active.title, "stage": "正在进入 OA 详情页并识别附件"})
                    session.commit()

            returncode, _stdout, stderr = _run_piped(command, Path.cwd(), heartbeat, poll_interval=2.0)
            if returncode == 5 and browser_restarts < 3:
                # The CLI restored the interrupted target to its pre-attempt
                # state. A new child process gives the next try a fresh browser.
                browser_restarts += 1
                continue
            if returncode != 0:
                last_error = f"retry_exit_{returncode}"
                break
            updated_snapshot = self._manifest_retry_snapshot(job_id)
            if updated_snapshot.pending_keys == snapshot.pending_keys:
                last_error = "retry_made_no_durable_progress"
                break

        final_snapshot = self._manifest_retry_snapshot(job_id)
        with Session(self.engine) as session:
            job = session.get(OperationJob, job_id)
            if job is None:
                return
            job.progress_current = final_snapshot.progress_current
            job.progress_total = final_snapshot.total
            job.status = "completed" if final_snapshot.complete and last_error is None else "failed"
            job.last_error_code = last_error or (None if final_snapshot.failed == 0 else "manifest_retry_item_failures")
            job.finished_at = datetime.now(timezone.utc)
            job.lease_owner = None
            job.lease_expires_at = None
            self._event(session, job, "manifest_retry_finished", job.status, {
                "processed": final_snapshot.progress_current,
                "failed": final_snapshot.failed,
                "remaining": len(final_snapshot.pending_keys),
                "stderr_tail": (stderr or "")[-2000:] if last_error is not None else "",
            })
            session.commit()

    def run_forever(self, poll_seconds: float = 2.0) -> None:
        self.recover_expired()
        from oa_knowledge.curation.classifier import PROMPT_VERSION
        from oa_knowledge.curation.rules import RULES_VERSION
        from oa_knowledge.curation.schemas import SCHEMA_VERSION
        self.production_queue.enqueue_stale_curation(
            rules_version=RULES_VERSION,
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
        )
        while True:
            # The worker process is persistent, but an OA browser session is
            # scoped to one operation and closed by BrowserSession.__exit__.
            # Keep that distinction visible while the scheduler waits.
            self._write_runtime_status("idle", "当前未登录 OA，等待下次定时任务")
            if not self.run_once():
                time.sleep(poll_seconds)

    def _write_runtime_status(self, status: str = "idle", activity: str | None = None) -> None:
        payload = json.dumps({
            "owner": self.owner,
            "pid": os.getpid(),
            "status": status,
            "activity": activity,
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        }).encode("utf-8")
        destination = atomic_write_bytes(payload, self.settings.state_root, "runtime/operation-worker.json")
        os.chmod(destination, 0o600)

    def _claim_next(self) -> int | None:
        now = datetime.now(timezone.utc)
        with Session(self.engine) as session:
            ready_realtime = session.scalar(select(PipelineTask.id).where(
                PipelineTask.queue_name.in_(("realtime_pending", "realtime_done")),
                PipelineTask.status == "queued",
                or_(PipelineTask.next_retry_at.is_(None), PipelineTask.next_retry_at <= now),
            ).limit(1)) is not None
            priority = case(
                (and_(OperationJob.job_type == "full_manifest_retry", OperationJob.parameters_json.like('%"source_status": "download_failed"%')), 0),
                (OperationJob.job_type == "full_manifest_retry", 1),
                else_=2,
            )
            conditions = [
                OperationJob.status == "queued",
                or_(OperationJob.lease_expires_at.is_(None), OperationJob.lease_expires_at < now, OperationJob.lease_owner == self.owner),
                or_(
                    OperationJob.job_type.notin_(("online_audit", "verified_archive_migration")),
                    OperationJob.heartbeat_at.is_(None),
                    OperationJob.heartbeat_at < now - ONLINE_AUDIT_YIELD,
                ),
            ]
            if ready_realtime:
                conditions.append(OperationJob.job_type.notin_((
                    "online_audit", "verified_archive_migration", "data_governance",
                )))
            job = session.scalar(select(OperationJob).where(
                *conditions,
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
