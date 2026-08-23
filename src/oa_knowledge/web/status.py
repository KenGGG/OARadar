from __future__ import annotations

import json
import hashlib
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from oa_knowledge.batches import BatchPlan, plan_batch
from oa_knowledge.config import Settings
from oa_knowledge.constants import BatchStatus
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.web.cli_runner import run_cli
from oa_knowledge.db.models import (
    ArchivedFile,
    BatchItem,
    CollectionBatch,
    ExclusionPolicy,
    ExclusionPolicyRevision,
    OAItem,
    OAManifestItem,
    OAManifestSync,
    OperationEvent,
    OperationJob,
    PipelineEvent,
    PipelineTask,
    ReviewEntry,
    Run,
)
from oa_knowledge.ops.audit import audit_database
from oa_knowledge.ops.capacity import scale_capacity_report
from oa_knowledge.ops.doctor import run_doctor
from oa_knowledge.full_manifest import classify_manifest_rows, effective_exclusion_keywords, export_manifest_csv, manifest_counts


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def full_manifest_status(settings: Settings) -> dict[str, Any]:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            latest = session.scalar(select(OAManifestSync).order_by(OAManifestSync.id.desc()).limit(1))
            counts = manifest_counts(session)
            job = session.scalar(
                select(OperationJob)
                .where(
                    OperationJob.job_type.in_(("full_manifest", "full_manifest_retry")),
                    OperationJob.status.in_(("running", "queued")),
                )
                .order_by(case((OperationJob.status == "running", 0), else_=1), OperationJob.id.desc())
                .limit(1)
            )
            active = None
            active_event = None
            if job and job.status == "running":
                latest_event = session.scalar(select(OperationEvent).where(OperationEvent.job_id == job.id).order_by(OperationEvent.sequence.desc()).limit(1))
                if latest_event and latest_event.event_type == "manifest_retry_item_started":
                    active_event = latest_event
                if active_event:
                    event_details = json.loads(active_event.details_json or "{}")
                    active = session.get(OAManifestItem, event_details.get("manifest_id")) if event_details.get("manifest_id") else None
            active_counts = _attachment_counts(session, [active.oa_item_key]) if active else {}
            active_attachment_counts = active_counts.get(active.oa_item_key, {}) if active else {}
            active_retrying_count = active_attachment_counts.get("failed", 0) if active else 0
            failure_count = 0
            if job and job.started_at:
                target_keys = list(json.loads(job.parameters_json or "{}").get("oa_item_keys") or [])
                if target_keys:
                    failure_count = session.scalar(
                        select(func.count()).select_from(OAManifestItem).where(
                            OAManifestItem.oa_item_key.in_(target_keys),
                            OAManifestItem.processing_status == "download_failed",
                            OAManifestItem.last_retry_at.is_not(None),
                            OAManifestItem.last_retry_at >= job.started_at,
                        )
                    ) or 0
            return counts | {
                "oa_total_count": latest.oa_total_count if latest else None,
                "manifest_status": latest.status if latest else "manifest_incomplete",
                "aligned": bool(latest and latest.status == "manifest_complete"),
                "pages_scanned": latest.pages_scanned if latest else 0,
                "source_total_pages": latest.source_total_pages if latest else None,
                "job": {
                    "id": job.id, "job_key": job.job_key, "status": job.status,
                    "progress_current": job.progress_current, "progress_total": job.progress_total or (latest.oa_total_count if latest else None),
                    "failure_count": failure_count,
                    "job_type": job.job_type,
                    "started_at": _iso_utc(job.started_at),
                    "heartbeat_at": _iso_utc(job.heartbeat_at),
                    "last_error_code": job.last_error_code,
                } if job else None,
                "active_item": {
                    "id": active.id,
                    "item_id": active.workitem_id_text or active.oa_item_key,
                    "title": active.title,
                    "sender": active.sender,
                    "status": "processing",
                    "attachment_total": active_attachment_counts.get("total", 0),
                    "attachment_success": active_attachment_counts.get("success", 0),
                    "attachment_failed": 0,
                    "attachment_pending": active_attachment_counts.get("pending", 0) + active_retrying_count,
                    "stage": (json.loads(active_event.details_json or "{}").get("stage") if active_event else None) or active.failure_stage or "正在进入 OA 详情页并识别附件",
                } if active else None,
            }
    finally:
        engine.dispose()


def start_full_manifest_job(settings: Settings) -> dict[str, Any]:
    """Queue the restart-safe page-by-page full manifest pipeline."""
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            active = session.scalar(select(OperationJob).where(
                OperationJob.job_type == "full_manifest",
                OperationJob.status.in_(("queued", "running", "paused")),
            ).order_by(OperationJob.id.desc()).limit(1))
            if active is not None:
                return {"job_id": active.id, "job_key": active.job_key, "status": active.status, "created": False}
            job = OperationJob(
                job_key=f"full-manifest-{uuid4().hex[:12]}", job_type="full_manifest", status="queued",
                idempotency_key=f"full-manifest-{uuid4().hex}", parameters_json="{}",
            )
            job.events.append(OperationEvent(sequence=1, event_type="created", status="queued", details_json="{}"))
            session.add(job); session.commit()
            return {"job_id": job.id, "job_key": job.job_key, "status": job.status, "created": True}
    finally:
        engine.dispose()


def start_done_incremental_job(settings: Settings) -> dict[str, Any]:
    """Queue one durable newest-three-pages Done refresh."""
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            active = session.scalar(select(OperationJob).where(
                OperationJob.job_type == "done_incremental",
                OperationJob.status.in_(("queued", "running")),
            ).order_by(OperationJob.id.desc()).limit(1))
            if active is not None:
                return {"job_id": active.id, "job_key": active.job_key, "status": active.status, "created": False}
            job = OperationJob(
                job_key=f"done-incremental-{uuid4().hex[:12]}",
                job_type="done_incremental",
                status="queued",
                idempotency_key=f"done-incremental-{uuid4().hex}",
                parameters_json=json.dumps({"max_pages": 3}),
            )
            job.events.append(OperationEvent(sequence=1, event_type="created", status="queued", details_json="{}"))
            session.add(job)
            session.commit()
            return {"job_id": job.id, "job_key": job.job_key, "status": job.status, "created": True}
    finally:
        engine.dispose()


def full_manifest_report_path(settings: Settings) -> Path:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            path = export_manifest_csv(session, settings.data_root)
            session.commit()
            return path
    finally:
        engine.dispose()


MANIFEST_LEDGER_STATUSES = (
    "downloaded", "no_attachment", "download_failed", "skipped", "processing", "pending_download",
)
ATTACHMENT_FILE_ROLES = ("direct_attachment", "official_attachment", "official_body", "opinion_attachment")


def _split_csv(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _display_manifest_status(status: str) -> str:
    # Older manifests used discovered before the ledger status vocabulary was finalized.
    return "pending_download" if status == "discovered" else status


def _manifest_filter_query(
    *,
    statuses: str | None = None,
    status: str | None = None,
    search: str | None = None,
    sender: str | None = None,
    keyword: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> Any:
    query = select(OAManifestItem)
    selected_statuses = _split_csv(statuses) or ([status] if status else [])
    if selected_statuses:
        raw_statuses = ["discovered" if value == "pending_download" else value for value in selected_statuses]
        query = query.where(OAManifestItem.processing_status.in_(raw_statuses))
    if search:
        term = f"%{search}%"
        query = query.where(or_(OAManifestItem.title.like(term), OAManifestItem.workitem_id_text.like(term), OAManifestItem.oa_item_key.like(term)))
    if sender:
        query = query.where(OAManifestItem.sender.like(f"%{sender}%"))
    if keyword:
        query = query.where(OAManifestItem.matched_exclusion_keyword.like(f"%{keyword}%"))
    if start_date:
        query = query.where(OAManifestItem.completed_at >= datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc))
    if end_date:
        query = query.where(OAManifestItem.completed_at < datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc) + timedelta(days=1))
    return query


def _attachment_counts(session: Session, oa_item_keys: list[str]) -> dict[str, dict[str, int]]:
    if not oa_item_keys:
        return {}
    rows = session.execute(
        select(
            OAItem.oa_item_key,
            func.count(ArchivedFile.id),
            func.sum(case((ArchivedFile.download_status == "verified", 1), else_=0)),
            func.sum(case((ArchivedFile.download_status.in_(("failed", "download_failed", "error")), 1), else_=0)),
            func.sum(case((ArchivedFile.download_status.in_(("discovered", "pending", "pending_download")), 1), else_=0)),
        )
        .join(ArchivedFile, ArchivedFile.oa_item_id == OAItem.id)
        .where(OAItem.oa_item_key.in_(oa_item_keys), ArchivedFile.file_role.in_(ATTACHMENT_FILE_ROLES))
        .group_by(OAItem.oa_item_key)
    ).all()
    return {
        key: {"total": total or 0, "success": success or 0, "failed": failed or 0, "pending": pending or 0}
        for key, total, success, failed, pending in rows
    }


def list_manifest_items(
    settings: Settings,
    page: int = 1,
    page_size: int = 20,
    status: str | None = None,
    search: str | None = None,
    statuses: str | None = None,
    sender: str | None = None,
    keyword: str | None = None,
    attachment_filter: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    sort: str = "completed_at",
    direction: str = "desc",
) -> dict[str, Any]:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            query = _manifest_filter_query(statuses=statuses, status=status, search=search, sender=sender, keyword=keyword, start_date=start_date, end_date=end_date)
            # The common path can stay in SQLite: filtering, counting, sorting and pagination
            # avoid materializing thousands of manifest rows on every keystroke.
            if not attachment_filter and sort in {"completed_at", "status", "retry_count", "last_processed_at"}:
                sort_columns = {
                    "completed_at": OAManifestItem.completed_at,
                    "status": OAManifestItem.processing_status,
                    "retry_count": OAManifestItem.retry_count,
                    "last_processed_at": func.coalesce(OAManifestItem.last_retry_at, OAManifestItem.last_synced_at),
                }
                sort_column = sort_columns[sort]
                order_column = sort_column.asc() if direction == "asc" else sort_column.desc()
                total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
                rows = session.scalars(query.order_by(order_column, OAManifestItem.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
                counts = _attachment_counts(session, [row.oa_item_key for row in rows])
                breakdown = dict(session.execute(select(OAManifestItem.processing_status, func.count()).group_by(OAManifestItem.processing_status)).all())
                summary = {status_key: int(breakdown.get(status_key, 0)) for status_key in MANIFEST_LEDGER_STATUSES}
                summary["pending_download"] += int(breakdown.get("discovered", 0))
                return _manifest_items_payload(rows, counts, page, page_size, total, breakdown, summary)
            prefiltered = session.scalars(query).all()
            counts = _attachment_counts(session, [row.oa_item_key for row in prefiltered])
            if attachment_filter:
                prefiltered = [row for row in prefiltered if _matches_attachment_filter(counts.get(row.oa_item_key, {}), attachment_filter)]
            reverse = direction != "asc"
            prefiltered.sort(key=lambda row: _manifest_sort_value(row, counts.get(row.oa_item_key, {}), sort), reverse=reverse)
            total = len(prefiltered)
            rows = prefiltered[(page - 1) * page_size:page * page_size]
            breakdown = dict(session.execute(select(OAManifestItem.processing_status, func.count()).group_by(OAManifestItem.processing_status)).all())
            summary = {status_key: int(breakdown.get(status_key, 0)) for status_key in MANIFEST_LEDGER_STATUSES}
            summary["pending_download"] += int(breakdown.get("discovered", 0))
            return _manifest_items_payload(rows, counts, page, page_size, total, breakdown, summary)
    finally:
        engine.dispose()


def _manifest_items_payload(rows: list[OAManifestItem], counts: dict[str, dict[str, int]], page: int, page_size: int, total: int, breakdown: dict[str, int], summary: dict[str, int]) -> dict[str, Any]:
    return {"items": [{
                "id": row.id, "item_id": row.workitem_id_text or row.oa_item_key, "title": row.title,
                "sender": row.sender, "completed_at": row.completed_at.isoformat() if row.completed_at else None,
                "list_page": row.list_page, "status": _display_manifest_status(row.processing_status),
                "matched_keyword": row.matched_exclusion_keyword, "archive_relpath": row.archive_relpath,
                "retry_count": row.retry_count, "last_error": row.last_error, "failure_stage": row.failure_stage,
                "last_error_summary": (row.last_error or "").splitlines()[0][:160] if row.last_error else None,
                "last_processed_at": (row.last_retry_at or row.last_synced_at).isoformat() if (row.last_retry_at or row.last_synced_at) else None,
                "needs_download": row.processing_status not in {"skipped", "no_attachment"},
                "attachment_total": counts.get(row.oa_item_key, {}).get("total", 0),
                "attachment_success": counts.get(row.oa_item_key, {}).get("success", 0),
                "attachment_failed": counts.get(row.oa_item_key, {}).get("failed", 0),
                "attachment_pending": counts.get(row.oa_item_key, {}).get("pending", 0),
        } for row in rows], "pagination": {"page": page, "page_size": page_size, "total": total, "total_pages": max(1, (total + page_size - 1) // page_size)}, "breakdown": breakdown, "summary": summary}


def _matches_attachment_filter(counts: dict[str, int], attachment_filter: str) -> bool:
    total = counts.get("total", 0)
    failed = counts.get("failed", 0)
    pending = counts.get("pending", 0)
    success = counts.get("success", 0)
    return {
        "has_attachment": total > 0,
        "no_attachment": total == 0,
        "has_failed": failed > 0,
        "all_success": total > 0 and failed == 0 and pending == 0 and success == total,
    }.get(attachment_filter, True)


def _manifest_sort_value(row: OAManifestItem, counts: dict[str, int], sort: str) -> Any:
    def comparable_time(value: datetime | None) -> datetime:
        if value is None:
            return datetime.min
        return value.replace(tzinfo=None)

    values = {
        "completed_at": comparable_time(row.completed_at),
        "status": row.processing_status,
        "attachment_total": counts.get("total", 0),
        "retry_count": row.retry_count,
        "last_processed_at": comparable_time(row.last_retry_at or row.last_synced_at),
    }
    return values.get(sort, values["completed_at"])


def manifest_item_detail(settings: Settings, manifest_id: int) -> dict[str, Any]:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            row = session.get(OAManifestItem, manifest_id)
            if row is None:
                raise LookupError("manifest item not found")
            item = session.scalar(select(OAItem).where(OAItem.oa_item_key == row.oa_item_key))
            files = [] if item is None else session.scalars(
                select(ArchivedFile).where(ArchivedFile.oa_item_id == item.id, ArchivedFile.file_role.in_(ATTACHMENT_FILE_ROLES))
                .order_by(ArchivedFile.file_role, ArchivedFile.original_name)
            ).all()
            summary = {
                "total": len(files),
                "success": sum(file.download_status == "verified" for file in files),
                "failed": sum(file.download_status in {"failed", "download_failed", "error"} for file in files),
                "pending": sum(file.download_status in {"discovered", "pending", "pending_download"} for file in files),
            }
            return {
                "id": row.id,
                "oa_item_key": row.oa_item_key,
                "item_id": row.workitem_id_text or row.oa_item_key,
                "title": row.title,
                "sender": row.sender,
                "completed_at": row.completed_at.isoformat() if row.completed_at else None,
                "status": _display_manifest_status(row.processing_status),
                "needs_download": row.processing_status not in {"skipped", "no_attachment"},
                "matched_keyword": row.matched_exclusion_keyword,
                "category": None,
                "archive_relpath": row.archive_relpath or (item.archive_relpath if item else None),
                "first_seen_at": row.first_seen_at.isoformat() if row.first_seen_at else None,
                "last_processed_at": (row.last_retry_at or row.last_synced_at).isoformat() if (row.last_retry_at or row.last_synced_at) else None,
                "retry_count": row.retry_count,
                "last_error": row.last_error,
                "failure_stage": row.failure_stage,
                "attachment_summary": summary,
                "status_message": _manifest_status_message(row),
                "attachments": [{
                    "id": file.id,
                    "original_name": file.original_name,
                    "file_type": file.mime_type or Path(file.original_name).suffix.lstrip(".").upper() or "未知",
                    "size_bytes": file.size_bytes,
                    "download_status": file.download_status,
                    "local_relpath": file.local_relpath,
                    "failure_reason": row.last_error if file.download_status in {"failed", "download_failed", "error"} else None,
                    "retry_count": file.download_attempts,
                } for file in files],
            }
    finally:
        engine.dispose()


def _manifest_status_message(row: OAManifestItem) -> str | None:
    if row.processing_status == "no_attachment":
        return "已检查OA详情页，确认没有附件"
    if row.processing_status == "skipped":
        keyword = row.matched_exclusion_keyword or "未知"
        return f"该事项命中排除关键词“{keyword}”，未进入详情页，因此没有下载正文和附件。"
    return None


def retry_manifest_failed_items(
    settings: Settings,
    search: str | None = None,
    statuses: str | None = None,
    sender: str | None = None,
    keyword: str | None = None,
    manifest_id: int | None = None,
    target_status: str = "download_failed",
    job_key_prefix: str = "manifest-retry",
) -> dict[str, Any]:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            query = _manifest_filter_query(statuses=statuses, search=search, sender=sender, keyword=keyword).where(OAManifestItem.processing_status == target_status)
            if manifest_id is not None:
                query = query.where(OAManifestItem.id == manifest_id)
            keys = [row.oa_item_key for row in session.scalars(query.order_by(OAManifestItem.retry_count.asc(), OAManifestItem.completed_at.desc(), OAManifestItem.id.desc())).all()]
            job = OperationJob(
                job_key=f"{job_key_prefix}-{uuid4().hex[:12]}",
                job_type="full_manifest_retry",
                status="queued",
                idempotency_key=f"manifest-retry-{uuid4().hex}",
                parameters_json=json.dumps({"oa_item_keys": keys, "source_status": target_status}, ensure_ascii=False),
                progress_total=len(keys),
            )
            job.events.append(OperationEvent(sequence=1, event_type="created", status="queued", details_json=json.dumps({"target_count": len(keys)})))
            session.add(job)
            session.commit()
            return {"job_id": job.id, "job_key": job.job_key, "status": job.status, "target_count": len(keys)}
    finally:
        engine.dispose()


def recheck_manifest_no_attachment(
    settings: Settings,
    search: str | None = None,
    sender: str | None = None,
    keyword: str | None = None,
    manifest_id: int | None = None,
) -> dict[str, Any]:
    return retry_manifest_failed_items(
        settings, search=search, sender=sender, keyword=keyword, manifest_id=manifest_id,
        target_status="no_attachment", job_key_prefix="manifest-no-attachment-recheck",
    )


def audit_all_manifest_items(settings: Settings) -> dict[str, Any]:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            keys = list(session.scalars(select(OAManifestItem.oa_item_key).order_by(OAManifestItem.id)))
            job = OperationJob(
                job_key=f"manifest-audit-all-{uuid4().hex[:12]}",
                job_type="full_manifest_retry",
                status="queued",
                idempotency_key=f"manifest-audit-all-{uuid4().hex}",
                parameters_json=json.dumps({"oa_item_keys": keys, "source_status": "audit_all"}, ensure_ascii=False),
                progress_total=len(keys),
            )
            job.events.append(OperationEvent(sequence=1, event_type="created", status="queued", details_json=json.dumps({"target_count": len(keys)})))
            session.add(job)
            session.commit()
            return {"job_id": job.id, "job_key": job.job_key, "status": job.status, "target_count": len(keys)}
    finally:
        engine.dispose()


def mark_manifest_manual_review(settings: Settings, manifest_id: int) -> dict[str, Any]:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            row = session.get(OAManifestItem, manifest_id)
            if row is None:
                raise LookupError("manifest item not found")
            item = session.scalar(select(OAItem).where(OAItem.oa_item_key == row.oa_item_key))
            row.failure_stage = "manual_review_required"
            row.last_error = row.last_error or "需要人工处理"
            review = ReviewEntry(
                kind="manual_review_required",
                item_id=item.id if item else None,
                details_json=json.dumps({"manifest_id": row.id, "oa_item_key": row.oa_item_key}, ensure_ascii=False),
            )
            session.add(review)
            session.commit()
            return {"status": "manual_review_required", "review_id": review.id}
    finally:
        engine.dispose()


def open_archived_file(settings: Settings, file_id: int, target: str = "file") -> dict[str, Any]:
    if target not in {"file", "directory"}:
        raise ValueError("target must be file or directory")
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            file = session.get(ArchivedFile, file_id)
            if file is None or not file.local_relpath:
                raise LookupError("file path not found")
            root = settings.data_root.resolve()
            path = (root / file.local_relpath).resolve()
            if root not in path.parents and path != root:
                raise ValueError("file path escapes data_root")
            target_path = path.parent if target == "directory" else path
            if not target_path.exists():
                raise FileNotFoundError(str(target_path))
            opener = shutil.which("xdg-open") or shutil.which("open")
            if opener is None:
                raise RuntimeError("no desktop opener available")
            subprocess.Popen([opener, str(target_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"opened": True, "target": target, "path": file.local_relpath}
    finally:
        engine.dispose()


def dashboard_status(settings: Settings) -> dict[str, Any]:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            counts = {
                "items": _count(session, OAItem),
                "files": _count(session, ArchivedFile),
                "batches": _count(session, CollectionBatch),
                "runs": _count(session, Run),
                "pending_reviews": session.scalar(
                    select(func.count()).select_from(ReviewEntry).where(ReviewEntry.status == "pending")
                ) or 0,
            }
            archive_counts = dict(
                session.execute(
                    select(BatchItem.archive_status, func.count())
                    .group_by(BatchItem.archive_status)
                ).all()
            )
            latest_batch = session.scalar(
                select(CollectionBatch).order_by(CollectionBatch.created_at.desc(), CollectionBatch.id.desc()).limit(1)
            )
            active_job = session.scalar(
                select(OperationJob)
                .where(OperationJob.status.in_(("queued", "running", "paused", "auth_required")))
                .order_by(OperationJob.created_at.desc())
                .limit(1)
            )
    finally:
        engine.dispose()

    runtime = _worker_runtime(settings)
    usage = shutil.disk_usage(settings.data_root)
    return {
        "service": "oa-knowledge-web",
        "stage": "2B-3",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema": _schema_version(settings),
        "oa_auth": {"status": "unknown", "checked_at": None, "read_only": True},
        "counts": {**counts, "archive_statuses": archive_counts},
        "batch": _batch_payload(latest_batch),
        "worker": _job_payload(active_job) or runtime,
        "worker_runtime": runtime,
        "storage": {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "used_percent": round(usage.used / usage.total * 100, 1) if usage.total else 0,
        },
    }


def _worker_runtime(settings: Settings) -> dict[str, Any] | None:
    path = settings.runtime_root / "operation-worker.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        heartbeat = datetime.fromisoformat(payload["heartbeat_at"])
        if (datetime.now(timezone.utc) - heartbeat).total_seconds() > 15:
            return None
        return {
            "key": payload["owner"], "type": "daemon", "status": payload["status"],
            "progress_current": 0, "progress_total": None,
            "heartbeat_at": payload["heartbeat_at"],
            "activity": payload.get("activity"),
        }
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def maintenance_status(settings: Settings, target_items: int = 500) -> dict[str, Any]:
    checks = run_doctor(settings)
    issues = audit_database(settings)
    capacity = scale_capacity_report(settings.database_path, settings.data_root, target_items)
    return {
        "doctor": {
            "ok": all(check.ok for check in checks if check.required),
            "checks": [check.__dict__ for check in checks],
        },
        "audit": {
            "ok": not issues,
            "issues": [issue.__dict__ for issue in issues],
        },
        "capacity": capacity.as_dict(),
    }


def list_reviews(
    settings: Settings,
    status: str = "pending",
    kind: str | None = None,
) -> list[dict[str, Any]]:
    if status not in {"pending", "resolved", "dismissed", "all"}:
        raise ValueError("invalid review status")
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            query = select(ReviewEntry).order_by(ReviewEntry.created_at.desc(), ReviewEntry.id.desc())
            if status != "all":
                query = query.where(ReviewEntry.status == status)
            if kind:
                query = query.where(ReviewEntry.kind == kind)
            rows = session.scalars(query).all()
            return [{
                "id": row.id, "kind": row.kind, "item_id": row.item_id,
                "file_id": row.file_id, "container_key": row.container_key,
                "depth": row.depth, "details": json.loads(row.details_json or "{}"),
                "status": row.status,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            } for row in rows]
    finally:
        engine.dispose()


def resolve_review(settings: Settings, review_id: int, resolution: str) -> dict[str, Any]:
    if resolution not in {"resolved", "dismissed"}:
        raise ValueError("resolution must be resolved or dismissed")
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            row = session.get(ReviewEntry, review_id)
            if row is None:
                raise LookupError("review entry not found")
            row.status = resolution
            session.commit()
            return {"id": row.id, "status": row.status}
    finally:
        engine.dispose()


def retry_source_review(settings: Settings, review_id: int) -> dict[str, Any]:
    """Recheck one manually-remediated source without retrying unrelated failures."""
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            review = session.get(ReviewEntry, review_id)
            if review is None:
                raise LookupError("review entry not found")
            if review.kind != "source_markdown_incomplete":
                raise ValueError("review entry is not a source markdown review")
            if review.item_id is None:
                raise ValueError("review entry is not linked to an OA item")
            item = session.get(OAItem, review.item_id)
            if item is None:
                raise LookupError("review OA item not found")
            task = session.scalar(
                select(PipelineTask)
                .where(
                    PipelineTask.logical_item_key == item.oa_item_key,
                    PipelineTask.stage == "source_publish",
                    PipelineTask.status == "failed",
                    PipelineTask.error_code.in_((
                        "UNSUPPORTED_SOURCE_FORMAT",
                        "PARSE_QUALITY_REJECTED",
                        "UNSAFE_SOURCE_PATH",
                    )),
                )
                .order_by(PipelineTask.priority, PipelineTask.updated_at.desc(), PipelineTask.id.desc())
                .limit(1)
            )
            if task is None:
                raise LookupError("failed source markdown task not found")
            task.stage = "attachment_inventory"
            task.status = "queued"
            task.attempts = 0
            task.error_code = None
            task.last_error = None
            task.recoverable = True
            task.next_retry_at = None
            task.finished_at = None
            task.lease_owner = None
            task.lease_expires_at = None
            review.status = "resolved"
            session.add(PipelineEvent(
                task_id=task.id,
                event_type="manual_review_retry",
                stage=task.stage,
                status=task.status,
                details_json=json.dumps({"review_id": review.id}),
            ))
            session.commit()
            return {"id": review.id, "status": review.status, "task_status": task.status}
    finally:
        engine.dispose()


def list_items(
    settings: Settings,
    page: int = 1,
    page_size: int = 20,
    pipeline_status_filter: str | None = None,
    source_channel: str | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    """Read-only paginated list of OA items for the management console."""
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            query = select(OAItem)
            if pipeline_status_filter:
                query = query.where(OAItem.pipeline_status == pipeline_status_filter)
            if source_channel:
                query = query.where(OAItem.source_channel == source_channel)
            if search:
                pattern = f"%{search}%"
                query = query.where(
                    OAItem.title.ilike(pattern)
                    | OAItem.sender.ilike(pattern)
                    | OAItem.oa_item_key.ilike(pattern)
                )
            total = session.scalar(select(func.count()).select_from(OAItem))
            if pipeline_status_filter:
                total = session.scalar(
                    select(func.count()).select_from(OAItem).where(OAItem.pipeline_status == pipeline_status_filter)
                )
            if source_channel:
                total = session.scalar(
                    select(func.count()).select_from(OAItem).where(OAItem.source_channel == source_channel)
                )
            if search:
                total_q = select(func.count()).select_from(OAItem).where(
                    OAItem.title.ilike(pattern) | OAItem.sender.ilike(pattern) | OAItem.oa_item_key.ilike(pattern)
                )
                total = session.scalar(total_q) or 0

            offset = max(0, page - 1) * page_size
            items = session.scalars(
                query.order_by(OAItem.last_seen_at.desc()).offset(offset).limit(page_size)
            ).all()

            # Pre-load file counts to avoid lazy-load after session close
            item_ids = [item.id for item in items]
            file_counts: dict[int, int] = {}
            if item_ids:
                for row in session.execute(
                    select(ArchivedFile.oa_item_id, func.count())
                    .where(ArchivedFile.oa_item_id.in_(item_ids))
                    .group_by(ArchivedFile.oa_item_id)
                ).all():
                    file_counts[row[0]] = row[1]

            status_breakdown = dict(
                session.execute(
                    select(OAItem.pipeline_status, func.count())
                    .group_by(OAItem.pipeline_status)
                ).all()
            )
            channel_breakdown = dict(
                session.execute(
                    select(OAItem.source_channel, func.count())
                    .group_by(OAItem.source_channel)
                ).all()
            )
    finally:
        engine.dispose()

    return {
        "items": [
            {
                "id": item.id,
                "oa_item_key": item.oa_item_key,
                "title": item.title,
                "sender": item.sender,
                "source_channel": item.source_channel,
                "pipeline_status": item.pipeline_status,
                "completed_at": item.completed_at.isoformat() if item.completed_at else None,
                "received_at": item.received_at.isoformat() if item.received_at else None,
                "archive_relpath": item.archive_relpath,
                "file_count": file_counts.get(item.id, 0),
            }
            for item in items
        ],
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": max(1, (total + page_size - 1) // page_size),
        },
        "breakdown": {
            "by_pipeline_status": status_breakdown,
            "by_source_channel": channel_breakdown,
        },
    }


def item_detail(settings: Settings, item_id: int) -> dict[str, Any]:
    """Return exactly what was archived for one OA item, without reading file contents."""
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            item = session.get(OAItem, item_id)
            if item is None:
                raise LookupError("item not found")
            files = session.scalars(
                select(ArchivedFile).where(ArchivedFile.oa_item_id == item.id)
                .order_by(ArchivedFile.depth, ArchivedFile.file_role, ArchivedFile.original_name)
            ).all()
            return {
                "id": item.id, "oa_item_key": item.oa_item_key, "title": item.title,
                "sender": item.sender, "department": item.department,
                "document_number": item.document_number, "source_channel": item.source_channel,
                "pipeline_status": item.pipeline_status, "archive_relpath": item.archive_relpath,
                "completed_at": item.completed_at.isoformat() if item.completed_at else None,
                "summary": {
                    "file_count": len(files),
                    "total_bytes": sum(file.size_bytes or 0 for file in files),
                    "verified_count": sum(file.download_status == "verified" for file in files),
                    "attachment_count": sum(file.file_role in {
                        "direct_attachment", "official_body", "official_attachment", "opinion_attachment"
                    } for file in files),
                },
                "files": [{
                    "id": file.id, "name": file.original_name, "file_role": file.file_role,
                    "mime_type": file.mime_type, "size_bytes": file.size_bytes,
                    "download_status": file.download_status, "depth": file.depth,
                    "container_key": file.source_container_key,
                    "local_relpath": file.local_relpath, "sha256": file.sha256,
                } for file in files],
            }
    finally:
        engine.dispose()


def start_archive_job(
    settings: Settings, batch_id: int, max_items: int, time_budget_seconds: int,
) -> dict[str, Any]:
    if not 1 <= max_items <= 20:
        raise ValueError("max_items must be between 1 and 20")
    if not 60 <= time_budget_seconds <= 1800:
        raise ValueError("time_budget_seconds must be between 60 and 1800")
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            batch = session.get(CollectionBatch, batch_id)
            if batch is None:
                raise LookupError("batch not found")
            if batch.status not in {BatchStatus.PAUSED, BatchStatus.READY, BatchStatus.RUNNING}:
                raise ValueError(f"batch cannot start from status: {batch.status}")
            active = session.scalar(select(OperationJob).where(
                OperationJob.job_type == "archive_batch",
                OperationJob.status.in_(("queued", "running")),
            ).limit(1))
            if active is not None:
                raise ValueError(f"an archive worker is already active: {active.job_key}")
            if batch.status == BatchStatus.PAUSED:
                batch.status = BatchStatus.RUNNING
            job = OperationJob(
                job_key=f"archive-{batch.id}-{uuid4().hex[:10]}", job_type="archive_batch",
                status="queued", idempotency_key=f"archive-{batch.id}-{uuid4().hex}",
                parameters_json=json.dumps({
                    "batch_id": batch.id, "batch_key": batch.batch_key,
                    "max_items": max_items, "time_budget_seconds": time_budget_seconds,
                }),
                progress_total=max_items,
            )
            job.events.append(OperationEvent(
                sequence=1, event_type="created", status="queued",
                details_json=json.dumps({"batch_key": batch.batch_key, "max_items": max_items}),
            ))
            session.add(job)
            session.commit()
            return {"job_id": job.id, "job_key": job.job_key, "status": job.status}
    finally:
        engine.dispose()


def archive_date_status(settings: Settings) -> dict[str, Any]:
    """Counts of Done archives by initiation-date correctness plus any active job."""
    from oa_knowledge.archive_reconciliation import reconciliation_counts

    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            counts = reconciliation_counts(session)
            active = session.scalar(select(OperationJob).where(
                OperationJob.job_type == "archive_date_reconcile",
                OperationJob.status.in_(("queued", "running", "paused")),
            ).order_by(OperationJob.id.desc()).limit(1))
            return {**counts, "job": job_progress(settings, active.id) if active else None}
    finally:
        engine.dispose()


def start_archive_date_job(settings: Settings) -> dict[str, Any]:
    """Queue a durable, read-only reconciliation of Done archives by initiation date."""
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            active = session.scalar(select(OperationJob).where(
                OperationJob.job_type == "archive_date_reconcile",
                OperationJob.status.in_(("queued", "running")),
            ).limit(1))
            if active is not None:
                raise ValueError(f"an archive-date reconciliation is already active: {active.job_key}")
            total = session.scalar(select(func.count()).select_from(OAItem).where(
                OAItem.source_channel == "done", OAItem.archive_relpath.is_not(None))) or 0
            job = OperationJob(
                job_key=f"archive-date-{uuid4().hex[:10]}",
                job_type="archive_date_reconcile",
                status="queued",
                idempotency_key=f"archive-date-{uuid4().hex}",
                parameters_json=json.dumps({}),
                progress_total=total,
            )
            job.events.append(OperationEvent(
                sequence=1, event_type="created", status="queued",
                details_json=json.dumps({"job_type": "archive_date_reconcile"}),
            ))
            session.add(job)
            session.commit()
            return {"job_id": job.id, "job_key": job.job_key, "status": job.status}
    finally:
        engine.dispose()


def set_archive_date_job_paused(settings: Settings, paused: bool) -> dict[str, Any]:
    """Pause or resume the active archive-date reconciliation job."""
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            job = session.scalar(select(OperationJob).where(
                OperationJob.job_type == "archive_date_reconcile",
                OperationJob.status.in_(("queued", "running", "paused")),
            ).order_by(OperationJob.id.desc()).limit(1))
            if job is None:
                raise LookupError("no active archive-date reconciliation job")
            job.status = "paused" if paused else "queued"
            session.commit()
            return {"job_id": job.id, "job_key": job.job_key, "status": job.status}
    finally:
        engine.dispose()


def start_backfill_campaign(
    settings: Settings,
    from_date: str = "2019-01-01",
    to_date: str = "2026-01-01",
    chunk_size: int = 20,
    time_budget_seconds: int = 1800,
) -> dict[str, Any]:
    """Queue the durable Stage 2A-7 campaign on the single OA worker."""
    from datetime import date

    start = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date)
    if start >= end:
        raise ValueError("from_date must be earlier than to_date")
    if not 1 <= chunk_size <= 20:
        raise ValueError("chunk_size must be between 1 and 20")
    if not 60 <= time_budget_seconds <= 1800:
        raise ValueError("time_budget_seconds must be between 60 and 1800")

    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            active = session.scalar(select(OperationJob).where(
                OperationJob.job_type == "backfill_campaign",
                OperationJob.status.in_(("queued", "running", "paused", "auth_required")),
            ).order_by(OperationJob.id.desc()).limit(1))
            if active is not None:
                resumed = active.status in {"paused", "auth_required"}
                if resumed:
                    active.status = "queued"
                    active.last_error_code = None
                    active.finished_at = None
                    active.lease_owner = None
                    active.lease_expires_at = None
                    sequence = (session.scalar(select(func.max(OperationEvent.sequence)).where(
                        OperationEvent.job_id == active.id,
                    )) or 0) + 1
                    active.events.append(OperationEvent(
                        sequence=sequence, event_type="resumed", status="queued",
                        details_json=json.dumps({"source": "backfill_start"}),
                    ))
                    session.commit()
                return {
                    "job_id": active.id, "job_key": active.job_key,
                    "status": active.status, "created": False, "resumed": resumed,
                }
            campaign_key = f"backfill-{from_date}-{to_date}"
            job = OperationJob(
                job_key=f"{campaign_key}-{uuid4().hex[:10]}",
                job_type="backfill_campaign", status="queued",
                idempotency_key=f"{campaign_key}-{uuid4().hex}",
                parameters_json=json.dumps({
                    "from_date": from_date, "to_date": to_date,
                    "chunk_size": chunk_size, "time_budget_seconds": time_budget_seconds,
                }),
            )
            job.events.append(OperationEvent(
                sequence=1, event_type="created", status="queued",
                details_json=json.dumps({"from_date": from_date, "to_date": to_date, "chunk_size": chunk_size}),
            ))
            session.add(job)
            session.commit()
            return {"job_id": job.id, "job_key": job.job_key, "status": job.status, "created": True, "resumed": False}
    finally:
        engine.dispose()


def execute_archive_job(settings: Settings, job_id: int, config_path: Path | None) -> None:
    """Execute one validated archive job through the existing bounded CLI runner."""
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            job = session.get(OperationJob, job_id)
            if job is None or job.status != "queued":
                return
            parameters = json.loads(job.parameters_json)
            job.status = "running"
            job.started_at = datetime.now(timezone.utc)
            job.heartbeat_at = job.started_at
            next_sequence = (session.scalar(select(func.max(OperationEvent.sequence)).where(OperationEvent.job_id == job.id)) or 0) + 1
            job.events.append(OperationEvent(sequence=next_sequence, event_type="started", status="running"))
            session.commit()
        returncode, payload = run_cli(
            [
                "batch", "run", parameters["batch_key"],
                "--max-items", str(parameters["max_items"]),
                "--time-budget-seconds", str(parameters["time_budget_seconds"]),
                "--operation-job-id", str(job_id),
            ],
            config_path,
            parameters["time_budget_seconds"] + 120,
        )
        with Session(engine) as session:
            job = session.get(OperationJob, job_id)
            if job is None:
                return
            job.progress_current = int(payload.get("processed", 0))
            run_status = str(payload.get("run_status", ""))
            if run_status == "auth_required":
                job.status = "auth_required"
            elif run_status == "item_failed":
                job.status = "failed"
            elif run_status in {"paused", "budget_exhausted"}:
                job.status = "paused"
            else:
                job.status = "completed" if returncode == 0 else "failed"
            job.last_error_code = (
                "item_failed" if run_status == "item_failed"
                else None if returncode == 0
                else (run_status or f"exit_{returncode}")
            )
            job.finished_at = datetime.now(timezone.utc)
            job.heartbeat_at = job.finished_at
            job.lease_owner = None
            job.lease_expires_at = None
            next_sequence = (session.scalar(select(func.max(OperationEvent.sequence)).where(OperationEvent.job_id == job.id)) or 0) + 1
            job.events.append(OperationEvent(
                sequence=next_sequence, event_type="finished", status=job.status,
                details_json=json.dumps({"processed": job.progress_current, "run_status": run_status}),
            ))
            session.commit()
    except (subprocess.TimeoutExpired, OSError) as exc:
        with Session(engine) as session:
            job = session.get(OperationJob, job_id)
            if job is not None:
                job.status = "failed"
                job.last_error_code = type(exc).__name__
                job.finished_at = datetime.now(timezone.utc)
                job.lease_owner = None
                job.lease_expires_at = None
                session.commit()
    finally:
        engine.dispose()


def pause_archive_batch(settings: Settings, batch_id: int) -> dict[str, Any]:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            batch = session.get(CollectionBatch, batch_id)
            if batch is None:
                raise LookupError("batch not found")
            if batch.status == BatchStatus.RUNNING:
                batch.status = BatchStatus.PAUSED
            elif batch.status != BatchStatus.PAUSED:
                raise ValueError(f"batch cannot pause from status: {batch.status}")
            session.commit()
            return {"batch_id": batch.id, "status": batch.status}
    finally:
        engine.dispose()


def resume_archive_batch(settings: Settings, batch_id: int) -> dict[str, Any]:
    """Resume a paused batch — moves it to running state."""
    from oa_knowledge.state import validate_transition
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            batch = session.get(CollectionBatch, batch_id)
            if batch is None:
                raise LookupError("batch not found")
            validate_transition(BatchStatus(batch.status), BatchStatus.RUNNING, {
                BatchStatus.PAUSED: {BatchStatus.RUNNING},
            })
            batch.status = BatchStatus.RUNNING
            session.commit()
            return {"batch_id": batch.id, "status": batch.status}
    finally:
        engine.dispose()


def freeze_archive_batch(settings: Settings, batch_id: int) -> dict[str, Any]:
    """Freeze a planned batch — locks the item list for discovery."""
    from oa_knowledge.state import validate_transition
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            batch = session.get(CollectionBatch, batch_id)
            if batch is None:
                raise LookupError("batch not found")
            if batch.status != BatchStatus.PLANNED:
                raise ValueError(f"only planned batches can be frozen, current: {batch.status}")
            if batch.frozen_at is None:
                batch.frozen_at = datetime.now(timezone.utc)
            session.commit()
            return {"batch_id": batch.id, "status": batch.status, "frozen": True}
    finally:
        engine.dispose()


def cancel_archive_batch(settings: Settings, batch_id: int) -> dict[str, Any]:
    """Cancel a planned batch that hasn't been frozen yet."""
    from oa_knowledge.state import validate_transition
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            batch = session.get(CollectionBatch, batch_id)
            if batch is None:
                raise LookupError("batch not found")
            if batch.frozen_at is not None:
                raise ValueError("frozen batch cannot be cancelled; preserve it for audit")
            validate_transition(BatchStatus(batch.status), BatchStatus.CANCELLED, {
                BatchStatus.PLANNED: {BatchStatus.CANCELLED},
            })
            batch.status = BatchStatus.CANCELLED
            batch.finished_at = datetime.now(timezone.utc)
            session.commit()
            return {"batch_id": batch.id, "status": batch.status}
    finally:
        engine.dispose()


def retry_batch_items(settings: Settings, batch_id: int) -> dict[str, Any]:
    """Retry all failed items in a batch — resets collect/download failures."""
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            batch = session.get(CollectionBatch, batch_id)
            if batch is None:
                raise LookupError("batch not found")
            failed = session.scalars(select(BatchItem).where(
                BatchItem.batch_id == batch.id,
                BatchItem.archive_status.in_({"collect_failed", "download_failed", "review_required"}),
            )).all()
            retried = 0
            for item in failed:
                item.archive_status = "pending"
                item.last_error = None
                retried += 1
            session.commit()
            return {"batch_id": batch.id, "retried": retried, "total_failed": len(failed)}
    finally:
        engine.dispose()


def batch_items_preview(settings: Settings, batch_id: int) -> dict[str, Any]:
    """Preview all items in a batch with their current status and policy classification."""
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            batch = session.get(CollectionBatch, batch_id)
            if batch is None:
                raise LookupError("batch not found")

            items = session.scalars(
                select(BatchItem)
                .where(BatchItem.batch_id == batch.id)
                .order_by(BatchItem.ordinal)
            ).all()

            # Get active policies for classification
            policies = session.scalars(
                select(ExclusionPolicy).where(ExclusionPolicy.enabled == True)
            ).all()

            # Classify items by policy
            metadata_only = []
            full_archive = []
            review_required = []
            skip_confirmed = []

            for item in items:
                if item.archive_status == "confirmed_skip":
                    skip_confirmed.append({
                        "ordinal": item.ordinal,
                        "title": item.title,
                        "skip_reason": item.skip_reason,
                        "policy_version": item.policy_version,
                    })
                    continue

                matched_policy = None
                for policy in policies:
                    if _policy_matches(
                        policy.pattern, policy.scope, item.title, item.sender, item.category
                    ):
                        matched_policy = policy
                        break

                if matched_policy:
                    if matched_policy.action == "metadata_only":
                        metadata_only.append({
                            "ordinal": item.ordinal,
                            "title": item.title,
                            "policy": matched_policy.name,
                        })
                    else:
                        full_archive.append({
                            "ordinal": item.ordinal,
                            "title": item.title,
                        })
                else:
                    full_archive.append({
                        "ordinal": item.ordinal,
                        "title": item.title,
                    })

            # Count files per item for budget estimate
            file_counts: dict[int, int] = {}
            if items:
                for row in session.execute(
                    select(ArchivedFile.oa_item_id, func.count())
                    .where(ArchivedFile.oa_item_id.in_([
                        i.oa_item_id for i in items if i.oa_item_id is not None
                    ]))
                    .group_by(ArchivedFile.oa_item_id)
                ).all():
                    file_counts[row[0]] = row[1]

            # Archive status breakdown
            status_breakdown = dict(
                session.execute(
                    select(BatchItem.archive_status, func.count())
                    .where(BatchItem.batch_id == batch.id)
                    .group_by(BatchItem.archive_status)
                ).all()
            )
    finally:
        engine.dispose()

    return {
        "batch_id": batch.id,
        "batch_key": batch.batch_key,
        "status": batch.status,
        "total_items": len(items),
        "policy_classification": {
            "metadata_only": len(metadata_only),
            "page_only": 0,
            "full_archive": len(full_archive),
            "skip_confirmed": len(skip_confirmed),
            "review_required": len(review_required),
        },
        "archive_status_breakdown": status_breakdown,
        "items": [
            {
                "ordinal": item.ordinal,
                "title": item.title,
                "workitem_id": item.workitem_id_text,
                "sender": item.sender,
                "archive_status": item.archive_status,
                "discovery_status": item.discovery_status,
                "policy_version": item.policy_version,
                "file_count": file_counts.get(item.oa_item_id, 0) if item.oa_item_id else 0,
                "skip_reason": item.skip_reason,
            }
            for item in items
        ],
    }


def job_progress(settings: Settings, job_id: int) -> dict[str, Any] | None:
    """Get current progress for a specific operation job."""
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            job = session.get(OperationJob, job_id)
            if job is None:
                return None
            events = session.scalars(
                select(OperationEvent)
                .where(OperationEvent.job_id == job.id)
                .order_by(OperationEvent.sequence)
            ).all()
            payload = {
                "id": job.id,
                "job_key": job.job_key,
                "job_type": job.job_type,
                "status": job.status,
                "progress_current": job.progress_current,
                "progress_total": job.progress_total,
                "lease_owner": job.lease_owner,
                "heartbeat_at": job.heartbeat_at.isoformat() if job.heartbeat_at else None,
                "last_error_code": job.last_error_code,
                "events": [
                    {
                        "sequence": e.sequence,
                        "event_type": e.event_type,
                        "status": e.status,
                        "details": json.loads(e.details_json) if e.details_json else {},
                        "created_at": e.created_at.isoformat() if e.created_at else None,
                    }
                    for e in events
                ],
            }
            if job.job_type == "backfill_campaign":
                payload["backfill"] = _backfill_progress_payload(session)
            return payload
    finally:
        engine.dispose()


def _backfill_progress_payload(session: Session) -> dict[str, Any]:
    batches = session.scalars(
        select(CollectionBatch)
        .where(CollectionBatch.notes.like("backfill:v1%"))
        .order_by(CollectionBatch.window_start.desc(), CollectionBatch.id.desc())
    ).all()
    active = next((batch for batch in batches if batch.status != BatchStatus.COMPLETED), None)

    discovered = sum(batch.discovered_count or 0 for batch in batches)
    source_rows = sum(batch.source_total_count or 0 for batch in batches)
    source_pages = sum(batch.source_total_pages or 0 for batch in batches)
    pages_scanned = sum(batch.pages_scanned or 0 for batch in batches)
    resolved_statuses = ("archived", "confirmed_skip", "review_required")
    resolved = session.scalar(
        select(func.count()).select_from(BatchItem).join(CollectionBatch).where(
            CollectionBatch.notes.like("backfill:v1%"),
            BatchItem.archive_status.in_(resolved_statuses),
        )
    ) or 0

    current_item = None
    current_page = None
    page_items: list[BatchItem] = []
    if active is not None:
        current_item = session.scalar(
            select(BatchItem).where(
                BatchItem.batch_id == active.id,
                BatchItem.archive_status == "archiving",
            ).order_by(BatchItem.ordinal).limit(1)
        )
        if current_item is None:
            current_item = session.scalar(
                select(BatchItem).where(
                    BatchItem.batch_id == active.id,
                    BatchItem.archive_status == "pending",
                ).order_by(BatchItem.ordinal).limit(1)
            )
        if current_item is not None:
            current_page = current_item.list_page or 1
        else:
            current_page = max(
                (item_page or 1 for item_page in session.scalars(
                    select(BatchItem.list_page).where(BatchItem.batch_id == active.id)
                )), default=1,
            )
        page_items = session.scalars(
            select(BatchItem).where(
                BatchItem.batch_id == active.id,
                func.coalesce(BatchItem.list_page, 1) == current_page,
            ).order_by(BatchItem.ordinal)
        ).all()

    oa_item_ids = [item.oa_item_id for item in page_items if item.oa_item_id is not None]
    attachment_counts: dict[int, int] = {}
    if oa_item_ids:
        attachment_counts = dict(session.execute(
            select(ArchivedFile.oa_item_id, func.count()).where(
                ArchivedFile.oa_item_id.in_(oa_item_ids),
                ArchivedFile.file_role.in_((
                    "direct_attachment", "official_attachment", "opinion_attachment",
                )),
                ArchivedFile.download_status == "verified",
            ).group_by(ArchivedFile.oa_item_id)
        ).all())
    page_resolved = sum(item.archive_status in resolved_statuses for item in page_items)
    recent_runs = session.scalars(
        select(Run).where(Run.stage == "2A-4", Run.finished_at.is_not(None))
        .order_by(Run.id.desc()).limit(10)
    ).all()
    recent_items = 0
    recent_seconds = 0.0
    last_finished_at = None
    for run in recent_runs:
        try:
            processed = int((json.loads(run.summary_json or "{}") or {}).get("processed", 0))
        except (TypeError, ValueError, json.JSONDecodeError):
            processed = 0
        if processed > 0 and run.finished_at is not None:
            recent_items += processed
            recent_seconds += max(0.0, (run.finished_at - run.started_at).total_seconds())
            last_finished_at = max(last_finished_at, run.finished_at) if last_finished_at else run.finished_at
    return {
        "discovered_items": discovered,
        "source_rows": source_rows,
        "source_pages": source_pages,
        "pages_scanned": pages_scanned,
        "resolved_items": resolved,
        "completed_batches": sum(batch.status == BatchStatus.COMPLETED for batch in batches),
        "detected_batches": len(batches),
        "recent_items_per_hour": round(recent_items / recent_seconds * 3600, 1) if recent_seconds else None,
        "last_item_finished_at": last_finished_at.isoformat() if last_finished_at else None,
        "active_batch": None if active is None else {
            "batch_key": active.batch_key,
            "window_start": active.window_start.isoformat() if active.window_start else None,
            "window_end": active.window_end.isoformat() if active.window_end else None,
            "discovered_items": active.discovered_count,
            "total_pages": active.source_total_pages,
            "pages_scanned": active.pages_scanned,
            "current_page": current_page,
            "page_resolved": page_resolved,
            "page_total": len(page_items),
            "current_ordinal": current_item.ordinal if current_item else None,
            "current_title": current_item.title if current_item else None,
            "items": [
                {
                    "ordinal": item.ordinal,
                    "title": item.title,
                    "status": item.archive_status,
                    "attachment_count": attachment_counts.get(item.oa_item_id, 0) if item.oa_item_id else 0,
                }
                for item in page_items
            ],
        },
    }


def latest_backfill_campaign(settings: Settings) -> dict[str, Any] | None:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            job_id = session.scalar(select(OperationJob.id).where(
                OperationJob.job_type == "backfill_campaign",
            ).order_by(OperationJob.id.desc()).limit(1))
    finally:
        engine.dispose()
    return job_progress(settings, job_id) if job_id is not None else None


def list_batches(settings: Settings) -> dict[str, Any]:
    """Read-only list of collection batches for the management console."""
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            batches = session.scalars(
                select(CollectionBatch)
                .order_by(CollectionBatch.created_at.desc())
                .limit(50)
            ).all()

            status_breakdown = dict(
                session.execute(
                    select(CollectionBatch.status, func.count())
                    .group_by(CollectionBatch.status)
                ).all()
            )
    finally:
        engine.dispose()

    return {
        "batches": [
            {
                "id": b.id,
                "batch_key": b.batch_key,
                "source_channel": b.source_channel,
                "window_start": b.window_start.isoformat() if b.window_start else None,
                "window_end": b.window_end.isoformat() if b.window_end else None,
                "status": b.status,
                "planned": b.planned_limit,
                "discovered": b.discovered_count,
                "archived": b.archived_count,
                "skipped": b.skipped_count,
                "failed": b.failed_count,
                "frozen": b.frozen_at is not None,
                "created_at": b.created_at.isoformat() if b.created_at else None,
            }
            for b in batches
        ],
        "status_breakdown": status_breakdown,
    }


def list_events(settings: Settings, since_job_id: int | None = None) -> list[dict[str, Any]]:
    """Read-only event stream data for SSE consumers."""
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            query = select(OperationEvent).order_by(OperationEvent.created_at.desc())
            if since_job_id is not None:
                query = query.where(OperationEvent.job_id == since_job_id)
            query = query.limit(100)
            events = session.scalars(query).all()
    finally:
        engine.dispose()

    return [
        {
            "id": e.id,
            "job_id": e.job_id,
            "event_type": e.event_type,
            "status": e.status,
            "details": json.loads(e.details_json) if e.details_json else {},
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in events
    ]


def _count(session: Session, model: type[Any]) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def _schema_version(settings: Settings) -> str:
    with sqlite3.connect(settings.database_path) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    return row[0] if row else "unversioned"


def _batch_payload(batch: CollectionBatch | None) -> dict[str, Any] | None:
    if batch is None:
        return None
    resolved = batch.archived_count + batch.skipped_count + batch.failed_count
    return {
        "key": batch.batch_key,
        "status": batch.status,
        "planned": batch.planned_limit,
        "discovered": batch.discovered_count,
        "archived": batch.archived_count,
        "skipped": batch.skipped_count,
        "failed": batch.failed_count,
        "pending": max(0, batch.discovered_count - resolved),
    }


def _job_payload(job: OperationJob | None) -> dict[str, Any] | None:
    if job is None:
        return None
    return {
        "key": job.job_key,
        "type": job.job_type,
        "status": job.status,
        "progress_current": job.progress_current,
        "progress_total": job.progress_total,
        "heartbeat_at": job.heartbeat_at.isoformat() if job.heartbeat_at else None,
    }


# ---- 2B-1: Discovery jobs and exclusion policies ----


def list_discovery_jobs(settings: Settings) -> list[dict[str, Any]]:
    """List all discovery jobs for the management console."""
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            jobs = session.scalars(
                select(OperationJob)
                .where(OperationJob.job_type == "discovery")
                .order_by(OperationJob.created_at.desc())
                .limit(50)
            ).all()
    finally:
        engine.dispose()

    return [
        {
            "id": j.id,
            "job_key": j.job_key,
            "status": j.status,
            "progress_current": j.progress_current,
            "progress_total": j.progress_total,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        }
        for j in jobs
    ]


def create_discovery_job(settings: Settings, source_channel: str = "done", days_back: int = 30) -> dict[str, Any]:
    """Create a title-only discovery job for the specified source channel.

    This triggers a batch plan that discovers done items but does NOT enter
    detail views or download attachments (metadata_only mode).
    """
    from datetime import time, timedelta
    from zoneinfo import ZoneInfo

    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            zone = ZoneInfo(settings.app.timezone)
            today = datetime.now(zone).date()
            window_start = datetime.combine(today - timedelta(days=days_back), time.min, zone)
            window_end = datetime.combine(today + timedelta(days=1), time.min, zone)

            batch, was_created = plan_batch(
                session,
                BatchPlan(
                    source_channel=source_channel,
                    window_start=window_start,
                    window_end=window_end,
                    window_field="completed_at",
                    planned_limit=500,
                    notes="title-only discovery from web console",
                ),
            )

            idempotency_key = f"discovery-{batch.plan_hash}"
            existing_job = session.scalar(select(OperationJob).where(OperationJob.idempotency_key == idempotency_key))
            if existing_job is not None:
                if existing_job.status in {"failed", "auth_required", "cancelled"}:
                    existing_job.status = "queued"
                    existing_job.last_error_code = None
                    existing_job.finished_at = None
                    existing_job.lease_owner = None
                    existing_job.lease_expires_at = None
                    next_sequence = (session.scalar(select(func.max(OperationEvent.sequence)).where(OperationEvent.job_id == existing_job.id)) or 0) + 1
                    existing_job.events.append(OperationEvent(sequence=next_sequence, event_type="requeued", status="queued"))
                    session.commit()
                return {
                    "job_id": existing_job.id, "job_key": existing_job.job_key,
                    "batch_id": batch.id, "batch_key": batch.batch_key,
                    "status": existing_job.status, "mode": "title_only",
                    "progress_current": existing_job.progress_current,
                    "progress_total": existing_job.progress_total,
                    "created": False,
                }

            job = OperationJob(
                job_key=f"discovery-{batch.batch_key}",
                job_type="discovery",
                status="queued",
                idempotency_key=idempotency_key,
                parameters_json=json.dumps({
                    "batch_id": batch.id,
                    "batch_key": batch.batch_key,
                    "source_channel": source_channel,
                    "mode": "title_only",
                    "days_back": days_back,
                }),
                progress_total=500,
            )
            session.add(job)
            session.flush()

            job.events.append(OperationEvent(
                sequence=1,
                event_type="created",
                status="queued",
                details_json=json.dumps({"mode": "title_only", "batch_key": batch.batch_key}),
            ))
            session.commit()

            return {
                "job_id": job.id,
                "job_key": job.job_key,
                "batch_id": batch.id,
                "batch_key": batch.batch_key,
                "status": "queued",
                "mode": "title_only",
                "progress_current": 0,
                "progress_total": 500,
                "created": True,
            }
    finally:
        engine.dispose()


def list_policies(settings: Settings) -> list[dict[str, Any]]:
    """List every exclusion policy used by classification."""
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            policies = session.scalars(
                select(ExclusionPolicy)
                .order_by(ExclusionPolicy.updated_at.desc())
            ).all()
    finally:
        engine.dispose()

    return [
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "pattern": p.pattern,
            "action": p.action,
            "scope": p.scope,
            "enabled": p.enabled,
            "version": p.version,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            "source": "database", "readonly": False,
        }
        for p in policies
    ]


def _reclassify_policy_matches(session: Session, old_pattern: str, data_root: Path) -> dict[str, int]:
    rows = session.scalars(select(OAManifestItem).where(
        OAManifestItem.processing_status == "skipped",
        OAManifestItem.matched_exclusion_keyword == old_pattern,
    )).all()
    classify_manifest_rows(session, rows, effective_exclusion_keywords(session), data_root)
    return {
        "affected_count": len(rows),
        "still_skipped_count": sum(row.processing_status == "skipped" for row in rows),
        "redownload_count": sum(row.processing_status in {"pending_download", "download_failed"} for row in rows),
        "reused_archive_count": sum(row.processing_status in {"downloaded", "no_attachment"} for row in rows),
    }


def create_policy(settings: Settings, name: str, pattern: str, action: str = "metadata_only",
                  scope: str = "title", description: str | None = None) -> dict[str, Any]:
    """Create/update a policy and immediately apply it to pending batch items."""
    name = name.strip()
    pattern = pattern.strip()
    if not name:
        raise ValueError("policy name must not be empty")
    if not pattern or len(pattern) > 120:
        raise ValueError("policy pattern must contain 1 to 120 characters")
    if action not in ("skip", "metadata_only"):
        raise ValueError(f"invalid action: {action}")
    if scope not in ("title", "sender", "category", "full"):
        raise ValueError(f"invalid scope: {scope}")

    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            existing = session.scalar(
                select(ExclusionPolicy).where(ExclusionPolicy.name == name)
            )
            old_pattern = existing.pattern if existing else None
            if existing:
                existing.pattern = pattern
                existing.action = action
                existing.scope = scope
                existing.description = description
                existing.enabled = True
                current_version = int(existing.version.removeprefix("v")) if existing.version.removeprefix("v").isdigit() else 1
                existing.version = f"v{current_version + 1}"
                existing.updated_at = datetime.now(timezone.utc)
            else:
                existing = ExclusionPolicy(
                    name=name,
                    description=description,
                    pattern=pattern,
                    action=action,
                    scope=scope,
                    enabled=True,
                    version="v1",
                )
                session.add(existing)
            session.flush()
            _record_policy_revision(session, existing, "updated" if existing.version != "v1" else "created")
            applied_count = _apply_policy_to_pending(session, existing)
            impact = _reclassify_policy_matches(session, old_pattern, settings.data_root) if old_pattern else {
                "affected_count": 0, "still_skipped_count": 0, "redownload_count": 0, "reused_archive_count": 0,
            }
            payload = {
                "id": existing.id,
                "name": existing.name,
                "description": existing.description,
                "pattern": existing.pattern,
                "action": existing.action,
                "scope": existing.scope,
                "enabled": existing.enabled,
                "version": existing.version,
                "created_at": existing.created_at.isoformat() if existing.created_at else None,
                "updated_at": existing.updated_at.isoformat() if existing.updated_at else None,
                "applied_count": applied_count,
                **impact,
            }
            session.commit()
            return payload
    finally:
        engine.dispose()


def create_policies_bulk(
    settings: Settings, text: str, action: str = "metadata_only", scope: str = "title",
) -> dict[str, Any]:
    """Create/update one policy per pasted line and apply all of them."""
    if action not in ("skip", "metadata_only"):
        raise ValueError(f"invalid action: {action}")
    if scope not in ("title", "sender", "category", "full"):
        raise ValueError(f"invalid scope: {scope}")
    patterns = _parse_policy_lines(text)
    if not patterns:
        raise ValueError("at least one keyword is required")
    if len(patterns) > 100:
        raise ValueError("a single import supports at most 100 keywords")

    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            results: list[dict[str, Any]] = []
            total_applied = 0
            for pattern in patterns:
                name = f"标题排除：{pattern}"
                policy = session.scalar(select(ExclusionPolicy).where(ExclusionPolicy.name == name))
                created = policy is None
                if policy is None:
                    policy = ExclusionPolicy(
                        name=name, pattern=pattern, action=action, scope=scope,
                        description="Web 批量录入", enabled=True, version="v1",
                    )
                    session.add(policy)
                else:
                    changed = policy.pattern != pattern or policy.action != action or policy.scope != scope or not policy.enabled
                    policy.pattern = pattern
                    policy.action = action
                    policy.scope = scope
                    policy.enabled = True
                    if changed:
                        current = int(policy.version.removeprefix("v")) if policy.version.removeprefix("v").isdigit() else 1
                        policy.version = f"v{current + 1}"
                    policy.updated_at = datetime.now(timezone.utc)
                session.flush()
                if created or changed:
                    _record_policy_revision(session, policy, "created" if created else "updated")
                applied = _apply_policy_to_pending(session, policy)
                total_applied += applied
                results.append({
                    "id": policy.id, "name": policy.name, "pattern": pattern,
                    "version": policy.version, "created": created, "applied_count": applied,
                })
            session.commit()
            return {
                "keyword_count": len(patterns),
                "created_count": sum(1 for result in results if result["created"]),
                "updated_count": sum(1 for result in results if not result["created"]),
                "applied_count": total_applied,
                "keywords": patterns,
                "policies": results,
            }
    finally:
        engine.dispose()


def update_title_policy(settings: Settings, policy_id: int, pattern: str) -> dict[str, Any] | None:
    """Edit one title exclusion policy without replacing its audit identity."""
    pattern = pattern.strip()
    if not pattern or len(pattern) > 120:
        raise ValueError("policy pattern must contain 1 to 120 characters")

    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            policy = session.get(ExclusionPolicy, policy_id)
            if policy is None:
                return None
            if policy.scope != "title":
                raise ValueError("only title exclusion policies can be edited here")

            old_pattern = policy.pattern
            policy.pattern = pattern
            policy.action = "skip"
            policy.enabled = True
            current_version = int(policy.version.removeprefix("v")) if policy.version.removeprefix("v").isdigit() else 1
            policy.version = f"v{current_version + 1}"
            policy.updated_at = datetime.now(timezone.utc)
            session.flush()
            _record_policy_revision(session, policy, "updated")
            applied_count = _apply_policy_to_pending(session, policy)
            impact = _reclassify_policy_matches(session, old_pattern, settings.data_root)
            payload = {
                "id": policy.id,
                "name": policy.name,
                "description": policy.description,
                "pattern": policy.pattern,
                "action": policy.action,
                "scope": policy.scope,
                "enabled": policy.enabled,
                "version": policy.version,
                "created_at": policy.created_at.isoformat() if policy.created_at else None,
                "updated_at": policy.updated_at.isoformat() if policy.updated_at else None,
                "applied_count": applied_count,
                **impact,
            }
            session.commit()
            return payload
    finally:
        engine.dispose()


def _parse_policy_lines(text: str) -> list[str]:
    patterns: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        value = re.sub(r"^\s*(?:[-*•]+|\d+[.)、])\s*", "", raw_line).strip()
        if not value:
            continue
        if len(value) > 120:
            raise ValueError(f"keyword exceeds 120 characters: {value[:20]}")
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            patterns.append(value)
    return patterns


def delete_policy(settings: Settings, policy_id: int) -> dict[str, Any] | None:
    """Delete an exclusion policy by ID."""
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            policy = session.get(ExclusionPolicy, policy_id)
            if policy is None:
                return None
            old_pattern = policy.pattern
            _record_policy_revision(session, policy, "deleted")
            session.delete(policy)
            session.flush()
            impact = _reclassify_policy_matches(session, old_pattern, settings.data_root)
            session.commit()
            return {"deleted": True, "id": policy_id, **impact}
    finally:
        engine.dispose()


def preview_policy_hits(settings: Settings, pattern: str, scope: str = "title",
                        limit: int = 50) -> dict[str, Any]:
    """Preview unique OA and frozen-batch items, including items not yet archived."""
    pattern = pattern.strip()
    if not pattern or len(pattern) > 120:
        raise ValueError("policy pattern must contain 1 to 120 characters")
    if scope not in ("title", "sender", "category", "full"):
        raise ValueError(f"invalid scope: {scope}")
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            hits_by_key: dict[str, dict[str, Any]] = {}
            for item in session.scalars(select(OAItem).order_by(OAItem.last_seen_at.desc())).all():
                if _policy_matches(pattern, scope, item.title, item.sender, None):
                    hits_by_key[item.oa_item_key] = {
                        "id": f"item-{item.id}", "oa_item_key": item.oa_item_key,
                        "title": item.title, "sender": item.sender,
                        "pipeline_status": item.pipeline_status, "source_channel": item.source_channel,
                    }
            for item in session.scalars(select(BatchItem).order_by(BatchItem.id.desc())).all():
                if item.oa_item_key in hits_by_key:
                    continue
                if _policy_matches(pattern, scope, item.title, item.sender, item.category):
                    hits_by_key[item.oa_item_key] = {
                        "id": f"batch-{item.id}", "oa_item_key": item.oa_item_key,
                        "title": item.title, "sender": item.sender,
                        "pipeline_status": item.archive_status, "source_channel": "done",
                    }
            all_hits = list(hits_by_key.values())
            status_counts: dict[str, int] = {}
            for item in all_hits:
                st = str(item["pipeline_status"])
                status_counts[st] = status_counts.get(st, 0) + 1
    finally:
        engine.dispose()

    return {
        "pattern": pattern,
        "scope": scope,
        "total_matches": len(all_hits),
        "sample_size": min(limit, len(all_hits)),
        "hits": all_hits[:limit],
        "status_breakdown": status_counts,
    }


def _policy_matches(
    pattern: str, scope: str, title: str | None, sender: str | None, category: str | None,
) -> bool:
    needle = pattern.casefold()
    values = {
        "title": (title,),
        "sender": (sender,),
        "category": (category,),
        "full": (title, sender, category),
    }[scope]
    return any(needle in (value or "").casefold() for value in values)


def _apply_policy_to_pending(session: Session, policy: ExclusionPolicy) -> int:
    pending = session.scalars(
        select(BatchItem).where(BatchItem.archive_status == "pending")
    ).all()
    affected_batches: set[int] = set()
    applied = 0
    for item in pending:
        if not _policy_matches(policy.pattern, policy.scope, item.title, item.sender, item.category):
            continue
        item.archive_status = "confirmed_skip"
        item.skip_reason = f"web_policy:{policy.id}:{policy.action}:{policy.pattern}"
        item.policy_version = f"exclusion-policy-{policy.id}-{policy.version}"
        item.last_error = None
        affected_batches.add(item.batch_id)
        applied += 1
    for batch_id in affected_batches:
        batch = session.get(CollectionBatch, batch_id)
        if batch is not None:
            batch.skipped_count = session.scalar(
                select(func.count()).select_from(BatchItem).where(
                    BatchItem.batch_id == batch_id,
                    BatchItem.archive_status == "confirmed_skip",
                )
            ) or 0
    return applied


def _record_policy_revision(
    session: Session, policy: ExclusionPolicy, change_type: str
) -> None:
    snapshot = json.dumps(
        {
            "id": policy.id,
            "name": policy.name,
            "description": policy.description,
            "pattern": policy.pattern,
            "action": policy.action,
            "scope": policy.scope,
            "enabled": policy.enabled,
            "version": policy.version,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    session.add(ExclusionPolicyRevision(
        policy_id=policy.id,
        policy_name=policy.name,
        version=policy.version,
        change_type=change_type,
        snapshot_json=snapshot,
        snapshot_sha256=hashlib.sha256(snapshot.encode("utf-8")).hexdigest(),
        actor="local_web",
    ))
