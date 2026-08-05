from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from time import monotonic
from typing import Callable
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import ArchivedFile, MarkdownExport, OAItem, OAManifestItem, OnlineAuditEvent, OnlineAuditItem, OnlineAuditRun, OperationEvent, OperationJob

ATTACHMENT_ROLES = ("direct_attachment", "official_attachment", "opinion_attachment")


@dataclass(frozen=True)
class AuditObservation:
    recognized_attachments: int


def unique_capture_attachment_count(capture) -> int:
    attachments = [row for row in capture.attachments if row.file_role in ATTACHMENT_ROLES]
    attachments += [row for container in capture.related_containers for row in container.attachments if row.file_role in ATTACHMENT_ROLES]
    return len({row.attachment_key for row in attachments})

def canonical_downloaded_count(*, recognized: int, verified_rows: int, unique_hashes: int) -> int:
    if verified_rows < recognized:
        return verified_rows
    return max(recognized, unique_hashes)


def classify_attachment_counts(recognized: int, database: int, downloaded: int, markdown: int) -> str:
    """Classify attachment inventory independently from downstream Markdown lag."""
    if recognized > downloaded:
        return "missing_download"
    if recognized < downloaded:
        return "historical_retained"
    return "matched"


def _event(session: Session, run_id: int, event_type: str, message: str, *, item_id: int | None = None, level: str = "info", details: dict | None = None) -> None:
    sequence = (session.scalar(select(func.max(OnlineAuditEvent.sequence)).where(OnlineAuditEvent.run_id == run_id)) or 0) + 1
    session.add(OnlineAuditEvent(run_id=run_id, item_id=item_id, sequence=sequence, event_type=event_type, level=level,
                                 message=message, details_json=json.dumps(details or {}, ensure_ascii=False)))


def _sanitize_error(exc: Exception) -> str:
    text = str(exc).replace("\n", " ")[:1000]
    text = re.sub(r"(?i)(authorization|cookie|token|secret)\s*[:=]?\s*[^;\s]+(?:\s+[^;\s]+)?", r"\1=[redacted]", text)
    return text


def start_audit(settings: Settings) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            active = session.scalar(select(OnlineAuditRun).where(OnlineAuditRun.status.in_(("queued", "running", "pause_requested", "paused"))).order_by(OnlineAuditRun.id.desc()).limit(1))
            if active:
                return {"run_id": active.id, "status": active.status, "total_items": active.total_items, "created": False}
            manifests = session.scalars(select(OAManifestItem).order_by(OAManifestItem.id)).all()
            job = OperationJob(job_key=f"online-audit-{uuid4().hex[:12]}", job_type="online_audit", status="queued",
                               idempotency_key=f"online-audit-{uuid4().hex}", parameters_json="{}", progress_total=len(manifests))
            job.events.append(OperationEvent(sequence=1, event_type="created", status="queued", details_json=json.dumps({"target_count": len(manifests)})))
            session.add(job); session.flush()
            run = OnlineAuditRun(job_id=job.id, status="queued", total_items=len(manifests))
            session.add(run); session.flush()
            session.add_all([OnlineAuditItem(run_id=run.id, manifest_item_id=row.id, oa_item_key=row.oa_item_key,
                workitem_id_text=row.workitem_id_text, title=row.title) for row in manifests])
            _event(session, run.id, "audit_created", f"在线审计已创建，共 {len(manifests)} 个已办事项")
            session.commit()
            return {"run_id": run.id, "status": run.status, "total_items": run.total_items, "created": True}
    finally:
        engine.dispose()


def pause_audit(settings: Settings, run_id: int) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            run = session.get(OnlineAuditRun, run_id)
            if not run or run.status not in {"queued", "running"}: raise ValueError("audit is not running")
            run.status = "pause_requested"; run.pause_requested_at = datetime.now(timezone.utc)
            job = session.get(OperationJob, run.job_id) if run.job_id else None
            if job and job.status == "queued": job.status = "paused"; run.status = "paused"
            _event(session, run.id, "pause_requested", "已请求安全暂停，将在当前事项完成后停止")
            session.commit(); return {"run_id": run.id, "status": run.status}
    finally: engine.dispose()


def resume_audit(settings: Settings, run_id: int) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            run = session.get(OnlineAuditRun, run_id)
            if not run or run.status not in {"paused", "pause_requested"}: raise ValueError("audit is not paused")
            run.status = "queued"; run.pause_requested_at = None
            job = session.get(OperationJob, run.job_id) if run.job_id else None
            if job: job.status = "queued"; job.finished_at = None; job.lease_owner = None; job.lease_expires_at = None
            _event(session, run.id, "audit_resumed", "在线审计已继续")
            session.commit(); return {"run_id": run.id, "status": run.status}
    finally: engine.dispose()


def restart_audit(settings: Settings) -> dict:
    """Preserve the previous ledger, supersede it, and create a fresh zero-progress run."""
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            active = session.scalar(select(OnlineAuditRun).where(OnlineAuditRun.status.in_(("queued", "running", "pause_requested", "paused"))).order_by(OnlineAuditRun.id.desc()).limit(1))
            if active:
                active.status = "superseded"; active.current_oa_item_key = None; active.finished_at = datetime.now(timezone.utc)
                job = session.get(OperationJob, active.job_id) if active.job_id else None
                if job:
                    job.status = "cancelled"; job.finished_at = active.finished_at; job.lease_owner = None; job.lease_expires_at = None
                _event(session, active.id, "audit_superseded", "旧审计已保留并由全新审计替代")
                session.commit()
    finally:
        engine.dispose()
    return start_audit(settings)


def fail_audit(settings: Settings, run_id: int, error_code: str) -> None:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            run = session.get(OnlineAuditRun, run_id)
            if not run: return
            run.status = "failed"; run.current_oa_item_key = None; run.finished_at = datetime.now(timezone.utc)
            _event(session, run.id, "audit_failed", "在线审计运行失败", level="error", details={"error_code": error_code})
            session.commit()
    finally: engine.dispose()


def _local_counts(session: Session, item: OnlineAuditItem) -> tuple[int, int, int]:
    oa = session.scalar(select(OAItem).where(OAItem.oa_item_key == item.oa_item_key))
    if not oa: return 0, 0, 0
    ids = list(session.scalars(select(ArchivedFile.id).where(ArchivedFile.oa_item_id == oa.id, ArchivedFile.file_role.in_(ATTACHMENT_ROLES))))
    total = len(ids)
    verified_rows = session.scalar(select(func.count()).select_from(ArchivedFile).where(ArchivedFile.oa_item_id == oa.id, ArchivedFile.file_role.in_(ATTACHMENT_ROLES), ArchivedFile.download_status == "verified")) or 0
    unique_hashes = session.scalar(select(func.count(func.distinct(ArchivedFile.sha256))).where(ArchivedFile.oa_item_id == oa.id, ArchivedFile.file_role.in_(ATTACHMENT_ROLES), ArchivedFile.download_status == "verified", ArchivedFile.sha256.is_not(None))) or 0
    downloaded = canonical_downloaded_count(recognized=item.recognized_attachments or 0, verified_rows=verified_rows, unique_hashes=unique_hashes)
    markdown = session.scalar(select(func.count()).select_from(MarkdownExport).where(MarkdownExport.source_file_id.in_(ids), MarkdownExport.status == "success")) if ids else 0
    return total, downloaded, markdown or 0


def execute_audit(settings: Settings, run_id: int, *, inspect_item: Callable[[OnlineAuditItem], AuditObservation]) -> None:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            run = session.get(OnlineAuditRun, run_id)
            if not run: raise LookupError("audit run not found")
            interrupted = session.scalars(select(OnlineAuditItem).where(OnlineAuditItem.run_id == run_id, OnlineAuditItem.status == "running")).all()
            for item in interrupted:
                item.status = "pending"; item.started_at = None
            if interrupted:
                _event(session, run.id, "items_recovered", f"已恢复 {len(interrupted)} 个中断事项")
            run.status = "running"; run.started_at = run.started_at or datetime.now(timezone.utc)
            _event(session, run.id, "audit_started", "开始在线遍历已办事项"); session.commit()
        while True:
            with Session(engine) as session:
                run = session.get(OnlineAuditRun, run_id); assert run
                if run.pause_requested_at:
                    run.status = "paused"; run.current_oa_item_key = None
                    job = session.get(OperationJob, run.job_id) if run.job_id else None
                    if job: job.status = "paused"; job.lease_owner = None; job.lease_expires_at = None
                    _event(session, run.id, "audit_paused", "在线审计已安全暂停"); session.commit(); return
                item = session.scalar(select(OnlineAuditItem).where(OnlineAuditItem.run_id == run_id, OnlineAuditItem.status == "pending").order_by(OnlineAuditItem.id).limit(1))
                if not item: break
                item.status = "running"; item.started_at = datetime.now(timezone.utc); run.current_oa_item_key = item.oa_item_key
                _event(session, run.id, "item_started", f"正在复查：{item.title}", item_id=item.id); session.commit()
                started = monotonic()
                try:
                    observation = inspect_item(item)
                    item = session.get(OnlineAuditItem, item.id); assert item
                    item.recognized_attachments = observation.recognized_attachments
                    item.database_attachments, item.downloaded_attachments, item.markdown_attachments = _local_counts(session, item)
                    from oa_knowledge.markdown_queue import enqueue_verified_for_oa
                    queued_markdown = enqueue_verified_for_oa(session, item.oa_item_key)
                    item.status = classify_attachment_counts(item.recognized_attachments, item.database_attachments, item.downloaded_attachments, item.markdown_attachments)
                    event_type, level = "item_completed", "info" if item.status == "matched" else "warning"
                    _event(session, run.id, event_type, f"复查完成：{item.title}", item_id=item.id,
                           level=level, details={"recognized": item.recognized_attachments, "downloaded": item.downloaded_attachments, "markdown": item.markdown_attachments, "markdown_queued": queued_markdown})
                except Exception as exc:
                    item = session.get(OnlineAuditItem, item.id); assert item
                    item.status = "access_failed"; item.error_code = "OA_ACCESS_ERROR"; item.error_detail = _sanitize_error(exc)
                    _event(session, run.id, "item_failed", f"OA访问失败：{item.title}", item_id=item.id, level="error", details={"error_code": item.error_code})
                item.finished_at = datetime.now(timezone.utc); item.elapsed_seconds = round(monotonic() - started, 3)
                groups = dict(session.execute(select(OnlineAuditItem.status, func.count()).where(OnlineAuditItem.run_id == run_id).group_by(OnlineAuditItem.status)).all())
                run = session.get(OnlineAuditRun, run_id); assert run
                run.completed_items = groups.get("matched", 0) + groups.get("missing_download", 0) + groups.get("historical_retained", 0) + groups.get("access_failed", 0)
                run.matched_items = groups.get("matched", 0)
                run.mismatch_items = groups.get("missing_download", 0)
                run.access_failed_items = groups.get("access_failed", 0)
                job = session.get(OperationJob, run.job_id) if run.job_id else None
                if job: job.progress_current = run.completed_items; job.heartbeat_at = datetime.now(timezone.utc)
                session.commit()
        with Session(engine) as session:
            run = session.get(OnlineAuditRun, run_id); assert run
            groups = dict(session.execute(select(OnlineAuditItem.status, func.count()).where(OnlineAuditItem.run_id == run_id).group_by(OnlineAuditItem.status)).all())
            run.completed_items = groups.get("matched", 0) + groups.get("missing_download", 0) + groups.get("historical_retained", 0) + groups.get("access_failed", 0)
            run.matched_items = groups.get("matched", 0)
            run.mismatch_items = groups.get("missing_download", 0)
            run.access_failed_items = groups.get("access_failed", 0)
            run.status = "completed"; run.current_oa_item_key = None; run.finished_at = datetime.now(timezone.utc)
            job = session.get(OperationJob, run.job_id) if run.job_id else None
            if job: job.status = "completed"; job.progress_current = run.completed_items; job.finished_at = run.finished_at; job.lease_owner = None; job.lease_expires_at = None
            _event(session, run.id, "audit_completed", "在线审计已完成"); session.commit()
    finally: engine.dispose()


def audit_view(settings: Settings, run_id: int | None = None, *, item_page: int = 1, item_page_size: int = 50) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            run = session.get(OnlineAuditRun, run_id) if run_id else session.scalar(select(OnlineAuditRun).order_by(OnlineAuditRun.id.desc()).limit(1))
            if not run:
                from oa_knowledge.markdown_queue import queue_view
                from oa_knowledge.web.status import archive_date_status
                return {"run": None, "items": [], "events": [], "errors": [], "markdown_queue": queue_view(settings), "archive_dates": archive_date_status(settings), "item_pagination": {"page": item_page, "page_size": item_page_size, "total": 0, "pages": 0}}
            item_total = session.scalar(select(func.count()).select_from(OnlineAuditItem).where(OnlineAuditItem.run_id == run.id)) or 0
            page_count = (item_total + item_page_size - 1) // item_page_size
            items = session.scalars(select(OnlineAuditItem).where(OnlineAuditItem.run_id == run.id).order_by(OnlineAuditItem.id).offset((item_page - 1) * item_page_size).limit(item_page_size)).all()
            events = session.scalars(select(OnlineAuditEvent).where(OnlineAuditEvent.run_id == run.id).order_by(OnlineAuditEvent.sequence.desc()).limit(200)).all()
            error_rows = session.execute(select(OnlineAuditItem.error_code, func.count()).where(OnlineAuditItem.run_id == run.id, OnlineAuditItem.error_code.is_not(None)).group_by(OnlineAuditItem.error_code)).all()
            missing_download_items = session.scalar(select(func.count()).select_from(OnlineAuditItem).where(OnlineAuditItem.run_id == run.id, OnlineAuditItem.status == "missing_download")) or 0
            local_extra_items = session.scalar(select(func.count()).select_from(OnlineAuditItem).where(OnlineAuditItem.run_id == run.id, OnlineAuditItem.status == "historical_retained")) or 0
            markdown_pending_items = session.scalar(select(func.count()).select_from(OnlineAuditItem).where(OnlineAuditItem.run_id == run.id, OnlineAuditItem.downloaded_attachments > OnlineAuditItem.markdown_attachments, OnlineAuditItem.status != "pending")) or 0
            from oa_knowledge.markdown_queue import queue_view
            from oa_knowledge.web.status import archive_date_status
            return {"run": {**{key: getattr(run, key) for key in ("id", "status", "total_items", "completed_items", "matched_items", "mismatch_items", "access_failed_items", "current_oa_item_key")},
                    "missing_download_items": missing_download_items, "local_extra_items": local_extra_items, "markdown_pending_items": markdown_pending_items,
                    "started_at": run.started_at.isoformat() if run.started_at else None, "finished_at": run.finished_at.isoformat() if run.finished_at else None},
                "items": [{key: getattr(row, key) for key in ("id", "oa_item_key", "title", "status", "recognized_attachments", "database_attachments", "downloaded_attachments", "markdown_attachments", "error_code", "error_detail", "elapsed_seconds")} for row in items],
                "events": [{"sequence": row.sequence, "event_type": row.event_type, "level": row.level, "message": row.message, "details": json.loads(row.details_json), "created_at": row.created_at.isoformat() if row.created_at else None} for row in events],
                "errors": [{"error_code": code, "count": count} for code, count in error_rows],
                "markdown_queue": queue_view(settings), "archive_dates": archive_date_status(settings), "item_pagination": {"page": item_page, "page_size": item_page_size, "total": item_total, "pages": page_count}}
    finally: engine.dispose()
