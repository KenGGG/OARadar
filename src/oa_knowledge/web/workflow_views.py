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

def update_item_classification(
    settings, item_id: int, *, content_origin: str,
    business_category: str | None, canonical_issuer: str | None, reason: str,
) -> dict:
    """Record a locked human decision and republish this item without OA access."""
    import hashlib
    from sqlalchemy.orm import sessionmaker
    from typing import get_args

    from oa_knowledge.classification.internal_classification import BusinessCategory
    from oa_knowledge.classification.per_item_classifier import CLASSIFIER_VERSION
    from oa_knowledge.classification.private_config import load_private_classification_config
    from oa_knowledge.classification.service import (
        ClassificationService, CreateClassificationRun, ManualDecisionCommand,
    )
    from oa_knowledge.production_pipeline import ProductionQueue

    reason = reason.strip()
    issuer = (canonical_issuer or "").strip()
    if not 3 <= len(reason) <= 400:
        raise ValueError("请填写 3 至 400 字的分类依据")
    if content_origin == "internal":
        if business_category not in get_args(BusinessCategory) or issuer:
            raise ValueError("内部事项必须选择一个业务分类，不能填写发文单位")
    elif content_origin == "external":
        if business_category or not 2 <= len(issuer) <= 80:
            raise ValueError("外部文件必须填写实际发文单位，不能选择内部业务分类")
    else:
        raise ValueError("请选择内部事项或外部文件")
    if settings.classification_private_dir is None:
        raise ValueError("分类私有配置不可用")
    loaded = load_private_classification_config(settings.classification_private_dir)
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            item = session.get(OAItem, item_id)
            if item is None or item.source_channel != "done":
                raise LookupError("已办事项不存在")
            manifest = session.scalar(select(OAManifestItem).where(OAManifestItem.oa_item_key == item.oa_item_key))
            if manifest is None or manifest.processing_status == "skipped" or (manifest.matched_exclusion_keyword or "").strip():
                raise ValueError("排除事项不能修改分类")
            if manifest.processing_status not in {"downloaded", "no_attachment"} or (
                manifest.processing_status == "no_attachment" and not manifest.no_attachment_confirmed
            ):
                raise ValueError("请先完成原件归档核验")
            current = session.scalar(select(ClassificationDecision).where(
                ClassificationDecision.oa_item_key == item.oa_item_key,
                ClassificationDecision.is_current.is_(True),
            ))
            item_key = item.oa_item_key
            initiator_type = current.initiator_type if current else "unknown"
            document_number = current.document_number if current else item.document_number
            document_type = current.document_type if current else None
        service = ClassificationService(sessionmaker(engine, expire_on_commit=False), loaded.config)
        run_id = f"manual-web-{uuid4().hex}"
        service.create_run(CreateClassificationRun(
            run_id=run_id, run_kind="incremental",
            manifest_sha256=hashlib.sha256(item_key.encode()).hexdigest(),
            exclusion_policy_sha256=hashlib.sha256(b"pipeline-exclusion-v1").hexdigest(),
            rule_version=CLASSIFIER_VERSION, schema_version="classification-v1",
            prompt_version="manual-web-v1", model_name=settings.llm.model,
            private_config_sha256=loaded.config_sha256, target_keys=(item_key,),
        ))
        decision = service.set_manual_decision(ManualDecisionCommand(
            run_id=run_id, oa_item_key=item_key, actor="local_web", reason=reason,
            classification_status="classified", content_origin=content_origin,
            business_category=business_category if content_origin == "internal" else None,
            canonical_issuer=issuer if content_origin == "external" else None,
            flow_type="formal_document" if content_origin == "external" else "approval",
            initiator_type=initiator_type, issuer=issuer if content_origin == "external" else None,
            document_number=document_number, document_type=document_type,
        ))
        with Session(engine) as session:
            active = session.scalar(select(PipelineTask).where(
                PipelineTask.logical_item_key == item_key,
                PipelineTask.stage.in_(LOCAL_STAGES),
                PipelineTask.status.in_(("queued", "running")),
            ).order_by(PipelineTask.id.desc()))
        task_id = active.id if active else ProductionQueue(engine).enqueue(
            "markdown_delivery", item_key, "classify",
            f"manual-classification:{decision.decision_id}",
            payload={"reason": "manual_classification"},
        )
        return {"status": "classified", "decision_id": decision.decision_id, "task_id": task_id}
    finally:
        engine.dispose()
