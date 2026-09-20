"""Item-scoped local evidence and recovery for the three business pages."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import ArchivedFile, ClassificationDecision, MarkdownExport, OAItem, OAManifestItem, ParseJob, PipelineEvent, PipelineTask
from oa_knowledge.done_archive import DONE_ARCHIVE_PREFIXES
from oa_knowledge.source_roles import MARKDOWN_SOURCE_ROLES
from oa_knowledge.storage_paths import resolve_data_path
from oa_knowledge.web.delivery_facts import delivery_facts

DELIVERY_LABELS = {"complete": "完整交付", "partial": "部分交付", "failed": "交付失败", "pending": "待处理", "working": "处理中", "needs_review": "待复核", "excluded": "已排除"}
LOCAL_STAGES = ("attachment_inventory", "parse", "source_publish", "classify", "index_publish")


def _iso(value):
    return value.isoformat() if value else None


def _tasks(session, key):
    return list(session.scalars(select(PipelineTask).where(
        PipelineTask.logical_item_key == key,
        PipelineTask.stage.in_((*LOCAL_STAGES, "done_capture_and_archive", "archive_verify")),
        PipelineTask.queue_name.in_(("markdown_delivery", "historical_done_backfill", "realtime_done")),
    ).order_by(PipelineTask.id.desc())))


def _task_view(task):
    return {"id": task.id, "stage": task.stage, "status": task.status,
            "error_code": task.error_code, "error": task.last_error,
            "recoverable": task.recoverable, "attempts": task.attempts,
            "next_retry_at": _iso(task.next_retry_at), "updated_at": _iso(task.updated_at)}


def _item_detail(session, settings, item):
    facts = delivery_facts(session, item)
    sources = list(session.scalars(select(ArchivedFile).where(ArchivedFile.oa_item_id == item.id).order_by(ArchivedFile.id)))
    exports = list(session.scalars(select(MarkdownExport).where(
        (MarkdownExport.oa_item_id == item.id) | MarkdownExport.source_file_id.in_([s.id for s in sources]),
    ).order_by(MarkdownExport.id)))
    tasks = _tasks(session, item.oa_item_key)
    local_active = any(t.stage in LOCAL_STAGES and t.status in {"queued", "running"} for t in tasks)
    latest_local = next((t for t in tasks if t.stage in LOCAL_STAGES), None)
    blocked = latest_local if latest_local and latest_local.status == "failed" and not latest_local.recoverable else None
    manifest = session.scalar(select(OAManifestItem).where(OAManifestItem.oa_item_key == item.oa_item_key))
    archive_ready = manifest is not None and (manifest.processing_status == "downloaded" or manifest.processing_status == "no_attachment" and manifest.no_attachment_confirmed)
    return {
        "id": item.id, "title": item.title, "manifest_id": manifest.id if manifest else None,
        "source_relpath": item.archive_relpath, "source_type": item.source_type or "unknown",
        "internal_category": item.internal_category, "external_issuer": item.external_issuer,
        "delivery": facts, "delivery_status": DELIVERY_LABELS[facts["status"]],
        "can_retry": archive_ready and not local_active and not blocked and facts["status"] not in {"excluded", "needs_review"},
        "retry_blocked_reason": "任务正在执行或排队" if local_active else "请先处理不可自动恢复的错误" if blocked else "请先完成原件校验" if not archive_ready else None,
        "tasks": [_task_view(t) for t in tasks[:20]],
        "files": [{"id": s.id, "name": s.original_name, "role": s.file_role,
                   "status": s.download_status, "size_bytes": s.size_bytes, "sha256": s.sha256,
                   "verified_at": _iso(s.verified_at), "relpath": s.local_relpath,
                   "download_url": f"/api/done-archives/files/{s.id}/download" if s.download_status == "verified" and s.local_relpath else None}
                  for s in sources],
        "documents": [{"id": e.id, "name": next((s.original_name for s in sources if s.id == e.source_file_id), "事项索引"),
                       "kind": e.document_kind, "relpath": e.markdown_relpath, "status": e.status,
                       "source_file_id": e.source_file_id, "engine": e.parse_engine,
                       "error_code": e.last_error_code, "error": e.last_error, "generated_at": _iso(e.generated_at)} for e in exports],
    }


def markdown_item_detail(settings, item_id):
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            item = session.get(OAItem, item_id)
            if item is None or item.source_channel != "done":
                raise LookupError("已办事项不存在")
            return _item_detail(session, settings, item)
    finally:
        engine.dispose()


def done_archive_detail(settings, manifest_id):
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            manifest = session.get(OAManifestItem, manifest_id)
            if manifest is None:
                raise LookupError("已办事项不存在")
            item = session.scalar(select(OAItem).where(OAItem.oa_item_key == manifest.oa_item_key, OAItem.source_channel == "done"))
            detail = _item_detail(session, settings, item) if item else {"files": [], "documents": [], "tasks": [_task_view(t) for t in _tasks(session, manifest.oa_item_key)], "id": None, "delivery": None}
            return {**detail, "manifest_id": manifest.id, "title": manifest.title,
                    "archive_status": manifest.processing_status, "archive_error": manifest.last_error,
                    "failure_stage": manifest.failure_stage, "last_synced_at": _iso(manifest.last_synced_at),
                    "no_attachment_confirmed": manifest.no_attachment_confirmed,
                    "can_retry_archive": manifest.processing_status in {"download_failed", "partial", "auth_required"} or manifest.processing_status == "no_attachment" and not manifest.no_attachment_confirmed}
    finally:
        engine.dispose()


def retry_markdown_item(settings, item_id):
    """Resume local production delivery; never recapture OA or overwrite originals."""
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            # Serialize read/check/write so repeated browser clicks share one task.
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            item = session.get(OAItem, item_id)
            if item is None or item.source_channel != "done":
                raise LookupError("已办事项不存在")
            manifest = session.scalar(select(OAManifestItem).where(OAManifestItem.oa_item_key == item.oa_item_key))
            if manifest is None or manifest.processing_status not in {"downloaded", "no_attachment"} or manifest.processing_status == "no_attachment" and not manifest.no_attachment_confirmed:
                raise ValueError("请先完成原件归档校验；深度受限事项不能标记完成")
            decision = session.scalar(select(ClassificationDecision).where(ClassificationDecision.oa_item_key == item.oa_item_key, ClassificationDecision.is_current.is_(True)))
            if decision and decision.classification_status in {"needs_review", "excluded"}:
                raise ValueError("当前分类需要复核或已排除，不能直接重新发布")
            tasks = [t for t in _tasks(session, item.oa_item_key) if t.stage in LOCAL_STAGES]
            active = next((t for t in tasks if t.status in {"queued", "running"}), None)
            if active:
                return {"task_id": active.id, "status": active.status, "enqueued": False}
            failed = tasks[0] if tasks and tasks[0].status == "failed" else None
            if failed and not failed.recoverable:
                raise ValueError("请先处理不可自动恢复的错误：" + (failed.error_code or "未知原因"))
            source_ids = select(ArchivedFile.id).where(ArchivedFile.oa_item_id == item.id, ArchivedFile.file_role.in_(MARKDOWN_SOURCE_ROLES))
            for job in session.scalars(select(ParseJob).where(ParseJob.file_id.in_(source_ids), ParseJob.status == "failed")):
                job.status = "queued"; job.error_code = None; job.attempts = 0
            task = failed or PipelineTask(queue_name="markdown_delivery", priority=50, logical_item_key=item.oa_item_key, idempotency_key=f"ui-markdown:{item.id}:{uuid4().hex}")
            task.stage = "attachment_inventory"; task.status = "queued"; task.attempts = 0
            task.error_code = None; task.last_error = None; task.next_retry_at = None
            task.finished_at = None; task.lease_owner = None; task.lease_expires_at = None
            session.add(task); session.flush()
            session.add(PipelineEvent(task_id=task.id, event_type="manual_retry", stage=task.stage, status=task.status))
            session.commit()
            return {"task_id": task.id, "status": task.status, "enqueued": True}
    finally:
        engine.dispose()


def markdown_document_path(settings, export_id):
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            export = session.get(MarkdownExport, export_id)
            if export is None or export.status != "success":
                raise LookupError("Markdown 尚未成功生成")
            # Export ledger paths are relative to the configured workspace.
            path = resolve_data_path(settings.workspace_root, export.markdown_relpath, allowed_prefixes=(settings.markdown_export.source_markdown_dir.as_posix(),))
            if path.suffix.lower() != ".md" or not path.is_file():
                raise LookupError("Markdown 文件不存在")
            return path
    finally:
        engine.dispose()


def archived_document_path(settings, file_id):
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            file = session.get(ArchivedFile, file_id)
            item = session.get(OAItem, file.oa_item_id) if file else None
            if not file or not item or item.source_channel != "done" or file.download_status != "verified" or not file.local_relpath:
                raise LookupError("已验证原件不存在")
            path = resolve_data_path(settings.data_root, file.local_relpath, allowed_prefixes=DONE_ARCHIVE_PREFIXES)
            if not path.is_file():
                raise LookupError("原件文件不存在")
            return path, file.original_name
    finally:
        engine.dispose()
