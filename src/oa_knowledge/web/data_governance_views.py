"""数据治理控制面的隐私安全读模型与异步任务入口。"""

from __future__ import annotations

import json
import shutil
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.data_governance.inventory import SUPPORTED_CATEGORIES
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import (
    ArchivedFile, CleanupItem, CleanupRun, OAItem, OperationJob, PipelineTask,
    ReviewEntry, Run,
)


def data_governance_view(settings: Settings) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            runs = session.scalars(select(CleanupRun).order_by(CleanupRun.id.desc()).limit(50)).all()
            integrity_run = session.scalar(select(Run).where(
                Run.stage == "integrity_reconciliation",
                Run.status == "completed",
            ).order_by(Run.id.desc()).limit(1))
            integrity = json.loads(integrity_run.summary_json) if integrity_run and integrity_run.summary_json else None
            if integrity is not None:
                integrity["finished_at"] = integrity_run.finished_at.isoformat() if integrity_run.finished_at else None
            category_summary: dict[str, dict[str, int]] = {}
            if runs:
                for category, count, bytes_total in session.execute(
                    select(
                        CleanupItem.category,
                        func.count(CleanupItem.id),
                        func.coalesce(func.sum(CleanupItem.size_bytes), 0),
                    )
                    .where(CleanupItem.cleanup_run_id == runs[0].id)
                    .group_by(CleanupItem.category)
                ):
                    category_summary[category] = {"count": count, "bytes": bytes_total}
            original_items = session.scalar(select(func.count(func.distinct(OAItem.id))).where(
                OAItem.source_channel == "done",
            )) or 0
            original_files, original_bytes = session.execute(
                select(
                    func.count(ArchivedFile.id),
                    func.coalesce(func.sum(ArchivedFile.size_bytes), 0),
                )
                .join(OAItem, OAItem.id == ArchivedFile.oa_item_id)
                .where(OAItem.source_channel == "done", ArchivedFile.download_status == "verified")
            ).one()
            active_tasks = session.scalar(select(func.count()).select_from(PipelineTask).where(
                PipelineTask.status.in_(("queued", "running", "retry_wait", "paused")),
            )) or 0
            pending_reviews = session.scalar(select(func.count()).select_from(ReviewEntry).where(
                ReviewEntry.status == "pending",
            )) or 0
            migration_job = session.scalar(select(OperationJob).where(
                OperationJob.job_type == "verified_archive_migration",
            ).order_by(OperationJob.id.desc()).limit(1))
            archive_migration = None
            if migration_job is not None:
                migration_params = json.loads(migration_job.parameters_json or "{}")
                archive_migration = {
                    "status": migration_job.status,
                    "progress_current": migration_job.progress_current,
                    "progress_total": migration_job.progress_total,
                    "migrated": int(migration_params.get("migrated", 0)),
                    "failed": int(migration_params.get("failed", 0)),
                    "review_required": int(migration_params.get("review_required", 0)),
                }
            quarantine_count, quarantine_bytes = session.execute(
                select(
                    func.count(CleanupItem.id),
                    func.coalesce(func.sum(CleanupItem.size_bytes), 0),
                ).where(CleanupItem.status == "quarantined")
            ).one()
            projection_bytes = category_summary.get("rebuildable_projection", {}).get("bytes", 0)
            temporary_bytes = sum(
                category_summary.get(name, {}).get("bytes", 0)
                for name in ("browser_cache", "runtime_reports", "expired_backups", "sent_pending_orphans")
            )
            usage = shutil.disk_usage(settings.data_root)
            storage = {
                "disk_total_bytes": usage.total,
                "disk_free_bytes": usage.free,
                "database_bytes": settings.database_path.stat().st_size if settings.database_path.exists() else 0,
                "active_tasks": active_tasks,
                "pending_reviews": pending_reviews,
                "category_summary": category_summary,
                "quarantine": {
                    "count": quarantine_count,
                    "bytes": quarantine_bytes,
                    "recoverable": True,
                },
                "tiers": [
                    {"id": "permanent", "label": "永久原件与账本", "retention": "永久保留", "count": original_files, "bytes": original_bytes, "database_references": original_files, "protected": True},
                    {"id": "active", "label": "活动中间产物", "retention": "任务完成前保留", "count": active_tasks, "bytes": 0, "database_references": active_tasks, "protected": True},
                    {"id": "projection", "label": "可重建投影", "retention": "可重建后隔离", "count": category_summary.get("rebuildable_projection", {}).get("count", 0), "bytes": projection_bytes, "database_references": 0, "protected": False},
                    {"id": "temporary", "label": "临时、缓存与过期备份", "retention": "按规则预检", "count": sum(category_summary.get(name, {}).get("count", 0) for name in ("browser_cache", "runtime_reports", "expired_backups", "sent_pending_orphans")), "bytes": temporary_bytes, "database_references": 0, "protected": False},
                ],
                "originals": {"items": original_items, "files": original_files, "bytes": original_bytes, "protected": True},
            }
            return {"integrity": integrity, "archive_migration": archive_migration, "storage": storage, "runs": [
                {
                    "id": run.id,
                    "status": run.status,
                    "rules_version": run.rules_version,
                    "categories": json.loads(run.categories_json),
                    "candidate_count": run.candidate_count,
                    "candidate_bytes": run.candidate_bytes,
                    "quarantined_count": run.quarantined_count,
                    "quarantined_bytes": run.quarantined_bytes,
                    "restored_count": run.restored_count,
                    "restored_bytes": run.restored_bytes,
                    "purged_count": run.purged_count,
                    "purged_bytes": run.purged_bytes,
                }
                for run in runs
            ]}
    finally:
        engine.dispose()


def enqueue_data_governance_plan(settings: Settings, categories: set[str]) -> dict:
    if not categories:
        raise ValueError("at least one cleanup category is required")
    unknown = categories - SUPPORTED_CATEGORIES
    if unknown:
        raise ValueError(f"unsupported cleanup categories: {','.join(sorted(unknown))}")
    return _enqueue(settings, {"action": "plan", "categories": sorted(categories)})


def enqueue_integrity_audit(settings: Settings) -> dict:
    return _enqueue(settings, {"action": "integrity_audit"})


def enqueue_data_governance_action(
    settings: Settings,
    run_id: int,
    action: str,
    *,
    confirmation: str | None = None,
) -> dict:
    if action not in {"quarantine", "restore", "purge"}:
        raise ValueError("unsupported data-governance action")
    if action == "purge" and confirmation != f"PURGE-CLEANUP-RUN-{run_id}":
        raise ValueError("exact purge confirmation is required")
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            if session.get(CleanupRun, run_id) is None:
                raise LookupError("cleanup run not found")
    finally:
        engine.dispose()
    parameters: dict[str, object] = {"action": action, "run_id": run_id}
    if confirmation is not None:
        parameters["confirmation"] = confirmation
    return _enqueue(settings, parameters)


def _enqueue(settings: Settings, parameters: dict[str, object]) -> dict:
    nonce = uuid4().hex
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            job = OperationJob(
                job_key=f"data-governance:{nonce}",
                job_type="data_governance",
                status="queued",
                idempotency_key=f"data-governance:{nonce}",
                parameters_json=json.dumps(parameters, sort_keys=True),
            )
            session.add(job)
            session.commit()
            return {"job_id": job.id, "status": job.status}
    finally:
        engine.dispose()
