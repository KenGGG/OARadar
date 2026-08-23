"""Console views for the product-aligned WebUI (plan-0807-1 §3-§10).

These wrap the existing read-only lifecycle/status functions and add the new
business-chain framing: 待办通知 / 已办归档 / Markdown 输出, plus a single
overview ``/api/dashboard``. Internal stage/status codes are localized here so
the frontend never has to display raw English codes (§11).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oa_knowledge.archive_paths import count_original_files, original_file_names
from oa_knowledge.config import Settings, load_settings, validate_feishu_runtime_config
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import (
    ArchivedFile, ContentObject, ItemOccurrence, KnowledgeDocument, LogicalItem,
    MarkdownExport, NotificationDelivery, OAItem, OAManifestItem, ParseArtifact,
    PipelineTask, SourceAttachment, SummaryVersion,
)
from oa_knowledge.notifications.feishu_service import retry_pending_summary_delivery
from oa_knowledge.pending_cleanup import delivery_for_occurrence, perform_cleanup, cleanup_eligibility
from oa_knowledge.markdown_queue import enqueue_file
from oa_knowledge.web.lifecycle_views import (
    done_list as lifecycle_done_list,
    knowledge_list as lifecycle_knowledge_list,
    pending_detail as lifecycle_pending_detail,
    pending_list as lifecycle_pending_list,
)
from oa_knowledge.web.provider_settings import provider_settings_view
from oa_knowledge.web.simple_status import (
    _classify_done_item,
    _done_simple_status_map,
    _SIMPLE_DONE_LABELS,
)
from oa_knowledge.web.status import dashboard_status, maintenance_status, retry_manifest_failed_items

# ---------------------------------------------------------------------------
# Status localization (§11). Raw internal codes -> operator-facing Chinese.
# ---------------------------------------------------------------------------

STATUS_LABELS: dict[str, str] = {
    "scheduled_hourly": "定时扫描",
    "done_capture_and_archive": "抓取并归档已办",
    "attachment_inventory": "核对附件清单",
    "notify_feishu": "发送飞书",
    "auth_required": "OA 登录失效",
    "retry_wait": "等待重试",
    "unknown_outcome": "发送结果待确认",
    "partial": "部分完成",
    "discovered": "待扫描",
    "scanning": "正在扫描",
    "downloaded": "完整归档",
    "no_attachment": "确认无附件",
    "download_failed": "下载失败",
    "queued": "排队中",
    "running": "处理中",
    "completed": "已完成",
    "failed": "失败",
    "sent": "已发送",
    "cleaned": "已清理",
    "cleaning": "清理中",
    "pending_cleanup": "等待清理",
    "not_eligible": "不适用",
    "cleanup_failed": "清理失败",
    "exported": "已交付",
    "export_failed": "交付失败",
}


def localize(status: str | None) -> str:
    if status is None:
        return "未知"
    return STATUS_LABELS.get(status, status)


def _counts(session: Session, model, *conditions) -> int:
    return session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0


def _delivery_status(session: Session, occurrence: ItemOccurrence) -> str | None:
    if occurrence.logical_item_id is None:
        return None
    delivery = session.scalar(select(NotificationDelivery).where(
        NotificationDelivery.logical_item_id == occurrence.logical_item_id,
        NotificationDelivery.channel == "feishu",
        NotificationDelivery.notification_type == "pending_summary",
    ).order_by(NotificationDelivery.id.desc()).limit(1))
    return delivery.status if delivery else None


_OA_JOB_ACTIVITY = {
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
}


def _oa_activity(base: dict, schedule: dict) -> dict:
    """Return a privacy-safe live OA activity card without titles or credentials."""
    runtime = base.get("worker_runtime") or {}
    if runtime.get("status") == "logging_in":
        return {
            "status": "logging_in", "label": "正在登录 OA",
            "detail": runtime.get("activity") or "正在验证 OA 登录",
            "heartbeat_at": runtime.get("heartbeat_at"), "progress_current": None,
            "progress_total": None,
        }
    if runtime.get("status") == "idle":
        return {
            "status": "disconnected", "label": "OA 已退出",
            "detail": runtime.get("activity") or "当前未登录 OA，等待下次定时任务",
            "heartbeat_at": runtime.get("heartbeat_at"), "progress_current": None,
            "progress_total": None,
        }
    worker = base.get("worker") or {}
    activity = runtime.get("activity") or _OA_JOB_ACTIVITY.get(worker.get("type"))
    if activity:
        return {
            "status": "working", "label": "正在处理 OA 工作",
            "detail": activity, "heartbeat_at": runtime.get("heartbeat_at") or worker.get("heartbeat_at"),
            "progress_current": worker.get("progress_current"), "progress_total": worker.get("progress_total"),
        }
    login = schedule.get("oa_login") or {}
    if login.get("status") == "authenticated":
        return {
            "status": "authenticated", "label": "OA 登录已验证",
            "detail": "当前没有 OA 任务，等待下次定时检查", "heartbeat_at": login.get("checked_at"),
            "progress_current": None, "progress_total": None,
        }
    return {
        "status": "unknown", "label": "OA 登录状态尚未确认",
        "detail": "Worker 正在等待任务或下一次定时检查", "heartbeat_at": runtime.get("heartbeat_at"),
        "progress_current": None, "progress_total": None,
    }


def _summary_status(session: Session, occurrence: ItemOccurrence) -> str:
    version = session.scalar(select(SummaryVersion).where(
        SummaryVersion.logical_item_id == occurrence.logical_item_id,
        SummaryVersion.summary_kind == "pending",
    ).order_by(SummaryVersion.id.desc()).limit(1))
    if version is None:
        return "pending"
    return version.status


# ---------------------------------------------------------------------------
# Dashboard / overview (§4)
# ---------------------------------------------------------------------------

def build_dashboard(settings: Settings) -> dict:
    from oa_knowledge.web.schedule_views import schedule_status

    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            base = dashboard_status(settings)
            schedule = schedule_status(settings)
            feishu_state = validate_feishu_runtime_config(settings)

            pending = session.scalars(select(ItemOccurrence).where(
                ItemOccurrence.channel == "pending",
                ItemOccurrence.occurrence_status == "active",
            )).all()
            statuses = [_delivery_status(session, o) for o in pending]
            feishu_sent = sum(1 for s in statuses if s == "sent")
            feishu_failed = sum(1 for s in statuses if s in {"failed", "rejected", "misconfigured"})
            cleanup_failed = sum(1 for o in pending if o.cleanup_status == "cleanup_failed")

            done = lifecycle_done_list(settings, page=1, page_size=1)
            metrics = done.get("metrics", {})
            download_failed = _counts(session, OAManifestItem, OAManifestItem.processing_status == "download_failed")

            total = _counts(session, MarkdownExport)
            # The conversion pipeline records a delivered Markdown file with status
            # "success" (see markdown_export/service.py); "exported" is only the
            # display label for that terminal state, never a stored status value.
            exported = _counts(session, MarkdownExport, MarkdownExport.status == "success")
            md_failed = _counts(session, MarkdownExport, MarkdownExport.status == "failed")
            md_pending = _counts(session, MarkdownExport, MarkdownExport.status == "pending")

            storage = base.get("storage", {})
            attention = _attention_list(
                settings, base, schedule, feishu_state, feishu_failed, cleanup_failed,
                storage.get("used_percent", 0),
            )

            pending_scan = schedule.get("last_job") or {}
            next_scan = schedule.get("next_run_at")

            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "pending_notification": {
                    "status": "normal" if not (feishu_failed or cleanup_failed) else "abnormal",
                    "last_scan_at": pending_scan.get("finished_at"),
                    "next_scan_at": next_scan,
                    "feishu_success": feishu_sent,
                    "feishu_failed": feishu_failed,
                    "awaiting_cleanup": sum(1 for o in pending if o.cleanup_status in {None, "not_eligible", "pending_cleanup"}),
                    "cleanup_failed": cleanup_failed,
                },
                "done_archive": {
                    "status": "normal" if download_failed == 0 else "abnormal",
                    "oa_done_total": metrics.get("oa_done_total", 0),
                    "downloaded_items": metrics.get("downloaded_items", 0),
                    "verified_attachments": metrics.get("verified_attachments", 0),
                    "download_failed": download_failed,
                },
                "markdown_delivery": {
                    "status": "normal" if md_failed == 0 else "abnormal",
                    "markdown_total": total,
                    "exported": exported,
                    "pending": md_pending,
                    "failed": md_failed,
                    "source_dir": str(settings.markdown_root),
                    "source_dir_exists": settings.markdown_root.exists(),
                    "source_dir_writable": os.access(settings.markdown_root, os.W_OK),
                },
                "oa_activity": _oa_activity(base, schedule),
                "needs_attention": attention,
            }
    finally:
        engine.dispose()


def _attention_list(
    settings: Settings, base: dict, schedule: dict, feishu_state: str,
    feishu_failed: int, cleanup_failed: int, used_percent: float,
) -> list[dict]:
    items: list[dict] = []
    if base.get("oa_auth", {}).get("status") == "auth_required":
        items.append({"code": "oa_login", "label": "OA 登录失效", "severity": "error", "jump": "maintenance"})
    if base.get("worker") is None:
        items.append({"code": "worker_stopped", "label": "Worker 已停止", "severity": "error", "jump": "maintenance"})
    if not schedule.get("enabled", True):
        items.append({"code": "timer_disabled", "label": "定时器未启用", "severity": "warning", "jump": "maintenance"})
    if feishu_state in {"missing_webhook", "missing_secret", "invalid_webhook"}:
        items.append({"code": "feishu_not_configured", "label": "飞书未配置", "severity": "warning", "jump": "settings"})
    if feishu_failed:
        items.append({"code": "feishu_failed", "label": f"{feishu_failed} 条飞书发送失败", "severity": "error", "jump": "pending"})
    if cleanup_failed:
        items.append({"code": "cleanup_failed", "label": f"{cleanup_failed} 条待办清理失败", "severity": "warning", "jump": "pending"})
    if used_percent and used_percent >= 90:
        items.append({"code": "disk_space", "label": f"磁盘空间不足（已用 {used_percent}%）", "severity": "error", "jump": "maintenance"})
    return items


# ---------------------------------------------------------------------------
# Pending notifications (§5)
# ---------------------------------------------------------------------------

_PENDING_FILTERS: dict[str, set[str]] = {
    "processing": {"active"},
    "summary_failed": {"failed"},
    "feishu_failed": {"failed", "rejected", "misconfigured"},
    "awaiting_cleanup": {"active", "cleaned"},
    "cleanup_failed": {"cleanup_failed"},
    "recent_success": {"cleaned"},
}


def pending_notifications_list(settings: Settings, filter_kind: str | None = None) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            stmt = select(ItemOccurrence).where(ItemOccurrence.channel == "pending")
            if filter_kind in {"processing", "summary_failed", "feishu_failed", "awaiting_cleanup", "cleanup_failed"}:
                stmt = stmt.where(ItemOccurrence.occurrence_status == "active")
            elif filter_kind == "recent_success":
                stmt = stmt.where(ItemOccurrence.occurrence_status == "cleaned")
            rows = session.scalars(stmt.order_by(ItemOccurrence.received_at.desc(), ItemOccurrence.id)).all()
            items = []
            for row in rows:
                delivery = _delivery_status(session, row)
                items.append({
                    "id": row.id,
                    "logical_item_id": row.logical_item_id,
                    "occurrence_key": row.occurrence_key,
                    "title": row.title,
                    "sender": row.sender,
                    "current_node": row.current_node,
                    "received_at": row.received_at.isoformat() if row.received_at else None,
                    "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
                    "summary_status": _summary_status(session, row),
                    "feishu_status": delivery or "pending",
                    "cleanup_status": row.cleanup_status or "not_eligible",
                    "occurrence_status": row.occurrence_status,
                    "cleaned_at": row.cleaned_at.isoformat() if row.cleaned_at else None,
                    "notify_fingerprint": row.notify_fingerprint,
                    "allow_renotify": row.allow_renotify,
                })
            if filter_kind in _PENDING_FILTERS:
                wanted = _PENDING_FILTERS[filter_kind]
                items = [
                    it for it in items
                    if (it["cleanup_status"] in wanted)
                    or (filter_kind == "feishu_failed" and it["feishu_status"] in wanted)
                    or (filter_kind == "summary_failed" and it["summary_status"] in wanted)
                ]
            return {"items": items, "total": len(items)}
    finally:
        engine.dispose()


def pending_notification_detail(settings: Settings, occurrence_id: int) -> dict:
    detail = lifecycle_pending_detail(settings, occurrence_id)
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            occurrence = session.get(ItemOccurrence, occurrence_id)
            if occurrence is not None:
                delivery = delivery_for_occurrence(session, occurrence)
                delivery_status = delivery.status if delivery is not None else "pending"
                detail["cleanup_status"] = occurrence.cleanup_status or "not_eligible"
                detail["cleaned_at"] = occurrence.cleaned_at.isoformat() if occurrence.cleaned_at else None
                detail["notify_fingerprint"] = occurrence.notify_fingerprint
                detail["allow_renotify"] = occurrence.allow_renotify
                detail["discovery_hash"] = occurrence.discovery_hash
                detail["occurrence_status"] = occurrence.occurrence_status
                detail["feishu_status"] = delivery_status
                detail["can_retry_delivery"] = delivery_status == "failed"
                detail["requires_delivery_reconciliation"] = delivery_status == "unknown"
                detail["can_cleanup"] = (
                    delivery_status == "sent"
                    and settings.pending_cleanup.auto_cleanup_after_success
                    and detail["cleanup_status"] not in {"cleaned", "cleaning"}
                )
                detail["oa_gone_at"] = occurrence.oa_gone_at.isoformat() if occurrence.oa_gone_at else None
                detail["stages"] = _pending_pipeline_stages(session, occurrence, detail)
    finally:
        engine.dispose()
    return detail


_PENDING_STAGE_ORDER = {
    "detail_sync": 0,
    "pending_parse": 1,
    "pending_summary": 2,
    "notify_feishu": 3,
}


def _pending_pipeline_stages(
    session: Session,
    occurrence: ItemOccurrence,
    detail: dict,
) -> dict[str, str]:
    tasks = list(session.scalars(select(PipelineTask).where(
        PipelineTask.queue_name == "realtime_pending",
        PipelineTask.logical_item_id == occurrence.logical_item_id,
        PipelineTask.stage.in_(tuple(_PENDING_STAGE_ORDER)),
    )))
    status_rank = {"running": 0, "queued": 1, "failed": 2, "completed": 3}
    tasks.sort(key=lambda row: (status_rank.get(row.status, 4), -row.id))
    task = tasks[0] if tasks else None

    def state(target: int) -> str:
        if task is None:
            return "pending"
        current = _PENDING_STAGE_ORDER.get(task.stage, -1)
        if current > target or (current == target and task.status == "completed"):
            return "done"
        if current < target:
            return "pending"
        if task.status == "running":
            return "running"
        if task.status == "failed":
            return "review" if not task.recoverable else "failed"
        return "pending"

    download = "done" if detail.get("snapshot") is not None or (task and _PENDING_STAGE_ORDER.get(task.stage, -1) > 0) else state(0)
    markdown = "done" if detail.get("ollama_summary_status") == "current" or (task and _PENDING_STAGE_ORDER.get(task.stage, -1) > 1) else state(1)
    summary = "done" if detail.get("ollama_summary_status") == "current" else state(2)
    delivery = detail.get("feishu_status")
    if delivery == "sent":
        feishu = "done"
    elif delivery in {"failed", "rejected", "misconfigured"}:
        feishu = "failed"
    elif delivery in {"unknown", "unknown_outcome"}:
        feishu = "review"
    else:
        feishu = state(3)
    cleanup_status = detail.get("cleanup_status")
    cleanup = (
        "done" if cleanup_status == "cleaned"
        else "failed" if cleanup_status == "cleanup_failed"
        else "pending"
    )
    return {
        "discovery": "done",
        "download": download,
        "markdown": markdown,
        "summary": summary,
        "feishu": feishu,
        "cleanup": cleanup,
    }


def retry_pending_summary(settings: Settings, occurrence_id: int) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            occurrence = session.get(ItemOccurrence, occurrence_id)
            if occurrence is None or occurrence.logical_item_id is None:
                raise LookupError("pending occurrence not found")
            from oa_knowledge.production_pipeline import ProductionQueue
            queue = ProductionQueue(engine)
            task_id = queue.enqueue(
                "realtime_pending", str(occurrence.logical_item_id), "pending_summary",
                f"retry-summary:{occurrence.logical_item_id}:{int(datetime.now(timezone.utc).timestamp())}",
                payload={"occurrence_id": occurrence_id, "force": True},
            )
            return {"task_id": task_id, "stage": "pending_summary", "status": "queued"}
    finally:
        engine.dispose()


def retry_pending_delivery(settings: Settings, occurrence_id: int) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            occurrence = session.get(ItemOccurrence, occurrence_id)
            if occurrence is None or occurrence.logical_item_id is None:
                raise LookupError("pending occurrence not found")
            delivery = session.scalar(select(NotificationDelivery).where(
                NotificationDelivery.logical_item_id == occurrence.logical_item_id,
                NotificationDelivery.channel == "feishu",
                NotificationDelivery.notification_type == "pending_summary",
            ).order_by(NotificationDelivery.id.desc()).limit(1))
            if delivery is None:
                raise LookupError("no delivery to retry")
            if delivery.status != "failed":
                raise ValueError(f"delivery retry requires confirmed failure: {delivery.status}")
            result = retry_pending_summary_delivery(engine, settings, delivery.id)
            return {"delivery_id": delivery.id, "status": result.status}
    finally:
        engine.dispose()


def cleanup_pending(settings: Settings, occurrence_id: int, *, force: bool = False) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            occurrence = session.get(ItemOccurrence, occurrence_id)
            if occurrence is None or occurrence.channel != "pending":
                raise LookupError("pending occurrence not found")
            if force and not settings.pending_cleanup.allow_force_cleanup:
                raise ValueError("force cleanup is disabled")
            now = datetime.now(timezone.utc)
            result = perform_cleanup(session, occurrence, settings, now, force=force)
            session.commit()
            return result
    finally:
        engine.dispose()


def sync_pending_occurrence(settings: Settings, occurrence_id: int) -> dict:
    """Enqueue an on-demand OA re-sync for one cleaned Pending occurrence.

    Refreshes only the display columns (title/sender/current_node/deadline) from
    OA without re-capturing or re-notifying (plan-0807-1 §sync). Returns the
    enqueued task id; an already-queued request returns the existing task id.
    """
    from oa_knowledge.production_pipeline import ProductionQueue

    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            occurrence = session.get(ItemOccurrence, occurrence_id)
            if occurrence is None or occurrence.channel != "pending":
                raise LookupError("pending occurrence not found")
            logical_item_id = occurrence.logical_item_id
            if logical_item_id is None:
                raise LookupError("pending occurrence has no logical item")
        queue = ProductionQueue(engine)
        idem = f"oa-resync:{occurrence_id}:{int(datetime.now(timezone.utc).timestamp())}"
        task_id = queue.enqueue(
            "realtime_pending", str(logical_item_id), "oa_resync", idem,
            payload={"occurrence_id": occurrence_id},
        )
        return {"task_id": task_id, "stage": "oa_resync", "status": "queued"}
    finally:
        engine.dispose()


def cleanup_eligible_pending(settings: Settings) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            now = datetime.now(timezone.utc)
            rows = session.scalars(select(ItemOccurrence).where(
                ItemOccurrence.channel == "pending",
                ItemOccurrence.occurrence_status == "active",
            )).all()
            cleaned = 0
            for occurrence in rows:
                delivery = delivery_for_occurrence(session, occurrence)
                eligible, _ = cleanup_eligibility(occurrence, delivery, settings, now)
                if eligible:
                    perform_cleanup(session, occurrence, settings, now)
                    cleaned += 1
            session.commit()
            return {"cleaned": cleaned}
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Done archives (§7) — split archive / markdown / handoff statuses
# ---------------------------------------------------------------------------

_ARCHIVE_STATUS_MAP = {
    "discovered": "待扫描", "scanning": "正在下载", "downloaded": "完整归档",
    "no_attachment": "确认无附件", "partial": "部分缺失", "download_failed": "下载失败",
}


def done_archives_list(
    settings: Settings, *, page: int = 1, page_size: int = 100,
    query: str | None = None, archive_status: str | None = None,
    markdown_status: str | None = None, handoff_status: str | None = None,
    simple_status: str | None = None,
) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            if simple_status is not None:
                # 服务端按简化状态筛选：先算出候选 manifest id 集合，再正确分页
                # 与计数，避免“先分页后过滤”导致 total / 页数错误（plan Task 2）。
                status_map = _done_simple_status_map(session)
                candidate_ids = [mid for mid, (state, _label, _reason) in status_map.items() if state == simple_status]
                stmt = select(OAManifestItem)
                if query:
                    pattern = f"%{query.strip()}%"
                    stmt = stmt.where(
                        OAManifestItem.title.ilike(pattern) | OAManifestItem.sender.ilike(pattern)
                    )
                if candidate_ids:
                    stmt = stmt.where(OAManifestItem.id.in_(candidate_ids))
                else:
                    stmt = stmt.where(False)
                total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
                rows = session.scalars(
                    stmt.order_by(OAManifestItem.list_page, OAManifestItem.list_ordinal, OAManifestItem.id)
                    .offset((page - 1) * page_size).limit(page_size)
                ).all()
                items = []
                for row in rows:
                    archived = session.scalar(select(OAItem).where(OAItem.oa_item_key == row.oa_item_key))
                    archive_relpath = row.archive_relpath or (archived.archive_relpath if archived else None)
                    file_count = count_original_files(settings.data_root, archive_relpath) if archive_relpath else None
                    simple = _enrich_done_item(session, settings, row, archived, None, None)
                    if simple is None:
                        continue
                    items.append({
                        "id": row.id,
                        "item_id": row.workitem_id_text,
                        "title": row.title,
                        "sender": row.sender,
                        "initiated_at": row.initiated_at.isoformat() if row.initiated_at else None,
                        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
                        "pipeline_status": row.processing_status,
                        "archive_relpath": row.archive_relpath or (archived.archive_relpath if archived else None),
                        "file_count": file_count,
                        "attachment_names": original_file_names(settings.data_root, archive_relpath),
                        "simple_status": simple["state"],
                        "simple_status_label": simple["label"],
                        "attention_reason": simple["reason"],
                        "updated_at": simple["updated_at"],
                    })
                return {
                    "items": items,
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                    "metrics": _done_list_metrics(settings, session),
                    "lifecycle_pilot_status": "validated" if total else "waiting_for_user_completion",
                }

            base = lifecycle_done_list(settings, page=page, page_size=page_size, query=query)
            enriched = []
            for it in base["items"]:
                manifest = session.get(OAManifestItem, it["id"])
                archive_status_label = _ARCHIVE_STATUS_MAP.get(manifest.processing_status, manifest.processing_status) if manifest else None
                md = _markdown_status_for_item(session, it, manifest)
                handoff = _handoff_status_for_item(settings, md)
                archived = session.scalar(select(OAItem).where(
                    OAItem.oa_item_key == manifest.oa_item_key
                )) if manifest else None
                stages = _done_pipeline_stages(session, manifest, archived, md)
                if archive_status and archive_status_label != archive_status:
                    continue
                if markdown_status and md["label"] != markdown_status:
                    continue
                if handoff_status and handoff["label"] != handoff_status:
                    continue
                single = _enrich_done_item(session, settings, manifest, archived, md, stages)
                if single is None:
                    continue
                enriched.append({
                    **it,
                    "archive_status_label": archive_status_label,
                    "markdown": md,
                    "handoff": handoff,
                    "stages": stages,
                    "local_dir": str(settings.archive_root / manifest.archive_relpath) if manifest and manifest.archive_relpath else None,
                    "simple_status": single["state"],
                    "simple_status_label": single["label"],
                    "attention_reason": single["reason"],
                    "updated_at": manifest.last_synced_at.isoformat() if manifest and manifest.last_synced_at else None,
                })
            base["items"] = enriched
            return base
    finally:
        engine.dispose()


def _done_list_metrics(settings: Settings, session: Session) -> dict:
    oa_done_total = _counts(session, OAManifestItem)
    downloaded_items = session.scalar(
        select(func.count(func.distinct(OAManifestItem.id))).select_from(OAManifestItem)
        .join(OAItem, OAItem.oa_item_key == OAManifestItem.oa_item_key)
        .where(OAManifestItem.processing_status == "downloaded")
    ) or 0
    archive_paths = session.execute(
        select(OAManifestItem.archive_relpath, OAItem.archive_relpath)
        .join(OAItem, OAItem.oa_item_key == OAManifestItem.oa_item_key, isouter=True)
    ).all()
    verified_attachments = sum(
        count_original_files(settings.data_root, archive_path)
        for archive_path in {manifest_path or item_path for manifest_path, item_path in archive_paths}
        if archive_path
    )
    return {
        "oa_done_total": oa_done_total,
        "downloaded_items": downloaded_items,
        "verified_attachments": verified_attachments,
    }


def _simple_done_state(
    session: Session, manifest: OAManifestItem | None, archived: OAItem | None, markdown: dict,
) -> dict[str, str | None]:
    """为单个已办事项计算简化状态（spec §4.1）。"""
    processing_status = manifest.processing_status if manifest is not None else "discovered"
    index = session.scalar(select(MarkdownExport).where(
        MarkdownExport.oa_item_id == (archived.id if archived is not None else None),
        MarkdownExport.document_kind == "item_index",
    ).order_by(MarkdownExport.id.desc()).limit(1))
    state, reason = _classify_done_item(
        processing_status=processing_status,
        has_success_item_index=bool(index and index.status == "success"),
        markdown_failed=bool(index and index.status == "failed"),
    )
    return {"state": state, "label": _SIMPLE_DONE_LABELS[state], "reason": reason}


def _enrich_done_item(
    session: Session, settings: Settings, manifest: OAManifestItem | None,
    archived: OAItem | None, md: dict | None, stages: dict | None,
) -> dict | None:
    """计算单个已办事项的简化状态字段（供两种分页路径复用）。"""
    if manifest is None:
        return None
    markdown = md or _markdown_status_for_item(session, {"id": manifest.id}, manifest)
    single = _simple_done_state(session, manifest, archived, markdown)
    return {
        "state": single["state"],
        "label": single["label"],
        "reason": single["reason"],
        "updated_at": manifest.last_synced_at.isoformat() if manifest.last_synced_at else None,
    }


_DONE_STAGE_ORDER = {
    "done_capture_and_archive": 0,
    "archive_verify": 1,
    "attachment_inventory": 2,
    "parse": 3,
    "source_publish": 4,
    "classify": 5,
    "index_publish": 6,
}


def _task_stage_state(task: PipelineTask | None, target: int) -> str:
    if task is None:
        return "pending"
    current = _DONE_STAGE_ORDER.get(task.stage, -1)
    if current > target or (current == target and task.status == "completed"):
        return "done"
    if current < target:
        return "pending"
    if task.status == "running":
        return "running"
    if task.status == "failed":
        return "review" if not task.recoverable else "failed"
    return "pending"


def _done_pipeline_stages(
    session: Session,
    manifest: OAManifestItem | None,
    archived: OAItem | None,
    markdown: dict,
) -> dict[str, str]:
    tasks = list(session.scalars(select(PipelineTask).where(
        PipelineTask.logical_item_key == (manifest.oa_item_key if manifest else ""),
        PipelineTask.stage.in_(tuple(_DONE_STAGE_ORDER)),
    )))
    status_rank = {"running": 0, "queued": 1, "failed": 2, "completed": 3}
    tasks.sort(key=lambda row: (
        status_rank.get(row.status, 4), row.priority, -row.id,
    ))
    task = tasks[0] if tasks else None
    manifest_status = manifest.processing_status if manifest else "discovered"
    if manifest_status in {"downloaded", "no_attachment"}:
        download = "done"
    elif manifest_status == "scanning":
        download = "running"
    elif manifest_status in {"partial", "download_failed"}:
        download = "failed"
    else:
        download = _task_stage_state(task, 0)
    verification = (
        "done" if manifest_status in {"downloaded", "no_attachment"}
        else "failed" if manifest_status in {"partial", "download_failed"}
        else _task_stage_state(task, 1)
    )
    task_index = _DONE_STAGE_ORDER.get(task.stage, -1) if task else -1
    if task and task_index > 4:
        markdown_state = "done"
    elif task and task_index in {3, 4}:
        markdown_state = _task_stage_state(task, task_index)
    elif markdown["status"] == "success":
        markdown_state = "done"
    elif markdown["status"] == "failed":
        markdown_state = "failed"
    else:
        markdown_state = "pending"

    classification = _task_stage_state(task, 5)
    publication = _task_stage_state(task, 6)
    return {
        "discovery": "done" if manifest is not None else "pending",
        "download": download,
        "verification": verification,
        "markdown": markdown_state,
        "classification": classification,
        "publication": publication,
    }


def _markdown_status_for_item(session: Session, it: dict, manifest: OAManifestItem | None) -> dict:
    if manifest is None:
        return {"label": "待转换", "status": "pending"}
    archived = session.scalar(select(OAItem).where(OAItem.oa_item_key == manifest.oa_item_key))
    if archived is None:
        return {"label": "待转换", "status": "pending"}
    exports = session.scalars(select(MarkdownExport).join(
        ArchivedFile, ArchivedFile.id == MarkdownExport.source_file_id
    ).where(ArchivedFile.oa_item_id == archived.id)).all()
    if not exports:
        return {"label": "待转换", "status": "pending"}
    if any(e.status == "failed" for e in exports):
        return {"label": "转换失败", "status": "failed"}
    if any(e.status == "success" for e in exports):
        return {"label": "转换成功", "status": "success"}
    if any(e.status in {"queued", "running"} for e in exports):
        return {"label": "转换中", "status": "running"}
    return {"label": "待转换", "status": "pending"}


def _handoff_status_for_item(settings: Settings, md: dict) -> dict:
    # A successfully converted Markdown file (status "success") is published to
    # markdown_root, so it counts as delivered. "exported" is the display label
    # only.
    if md["status"] != "success":
        return {"label": "待交付", "status": "pending"}
    if not settings.markdown_root.exists():
        return {"label": "来源目录不可用", "status": "unavailable"}
    return {"label": "已交付", "status": "exported"}


# ---------------------------------------------------------------------------
# Markdown outputs (§8)
# ---------------------------------------------------------------------------

def markdown_outputs_list(
    settings: Settings, *, page: int = 1, page_size: int = 50,
) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            total = _counts(session, MarkdownExport)
            exports = session.scalars(
                select(MarkdownExport)
                .order_by(MarkdownExport.generated_at.desc(), MarkdownExport.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            docs = []
            for export in exports:
                source_file = session.get(ArchivedFile, export.source_file_id) if export.source_file_id else None
                oa_item = session.get(OAItem, source_file.oa_item_id) if source_file and source_file.oa_item_id else None
                manifest = session.scalar(select(OAManifestItem).where(
                    OAManifestItem.oa_item_key == oa_item.oa_item_key
                )) if oa_item else None
                artifact = session.get(ParseArtifact, export.parse_artifact_id) if export.parse_artifact_id else None
                quality = artifact.quality_status if artifact else None
                docs.append({
                    "id": export.id,
                    "markdown_relpath": export.markdown_relpath,
                    "source_file": source_file.original_name if source_file else None,
                    "source_oa_item": manifest.title if manifest else None,
                    "engine": export.parse_engine,
                    "quality": quality or "unknown",
                    "generated_at": export.generated_at.isoformat() if export.generated_at else None,
                    "oaradar_path": str(settings.markdown_root / export.markdown_relpath),
                    "llm_wiki_path": str(settings.markdown_root / export.markdown_relpath),
                    "delivery_status": "exported" if export.status == "success" else export.status,
                })
            # V2 consumes Markdown by Done item, while retaining the legacy
            # attachment ledger payload above for existing clients during the
            # compatibility window.  No paths are made absolute or user supplied.
            item_total = session.scalar(select(func.count()).select_from(OAItem).where(
                OAItem.source_channel == "done",
            )) or 0
            items = []
            archived_items = session.scalars(select(OAItem).where(
                OAItem.source_channel == "done",
            ).order_by(OAItem.completed_at.desc(), OAItem.id.desc()).offset(
                (page - 1) * page_size
            ).limit(page_size)).all()
            for item in archived_items:
                item_exports = session.scalars(select(MarkdownExport).where(
                    MarkdownExport.oa_item_id == item.id,
                ).order_by(MarkdownExport.id)).all()
                # Older attachment exports did not have oa_item_id.  Read them
                # as facts without manufacturing a new association.
                if not item_exports:
                    item_exports = session.scalars(select(MarkdownExport).join(
                        ArchivedFile, ArchivedFile.id == MarkdownExport.source_file_id,
                    ).where(ArchivedFile.oa_item_id == item.id)).all()
                index = next((row for row in item_exports if row.document_kind == "item_index"), None)
                attachments = [row for row in item_exports if row.document_kind != "item_index"]
                if index and index.status == "success":
                    delivery_status = "已交付"
                elif any(row.status == "failed" for row in attachments) or (index and index.status == "failed"):
                    delivery_status = "交付失败"
                elif any(row.status == "success" for row in attachments):
                    delivery_status = "部分交付"
                else:
                    delivery_status = "待处理"
                items.append({
                    "id": item.id,
                    "title": item.title,
                    "source_type": item.source_type or "unknown",
                    "internal_category": item.internal_category,
                    "external_issuer": item.external_issuer,
                    "markdown_count": sum(row.status == "success" for row in attachments),
                    "delivery_status": delivery_status,
                    "index_relpath": index.markdown_relpath if index else None,
                    "source_relpath": item.archive_relpath,
                    "updated_at": (index.generated_at.isoformat() if index and index.generated_at else None),
                    "documents": [
                        {"id": row.id, "relpath": row.markdown_relpath, "status": row.status,
                         "source_file_id": row.source_file_id, "error_code": row.last_error_code}
                        for row in attachments
                    ],
                })
            return {
                "documents": docs, "total": total, "page": page, "page_size": page_size,
                "items": items, "item_total": item_total,
            }
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Settings (§9) — read grouped by business chain; write to YAML.
# ---------------------------------------------------------------------------

SETTINGS_SECTIONS = {
    "llm": {"enabled", "active_provider", "ollama_base_url", "ollama_model",
            "timeout_seconds", "max_tokens", "temperature", "max_retries", "max_concurrency"},
    "feishu": {"enabled", "message_type", "max_items_per_section", "redact_confidential", "retry_attempts",
               "webhook_env", "secret_env"},
    "pending_cleanup": {"auto_cleanup_after_success", "cleanup_delay_hours", "failed_retention_days",
                        "keep_summary_body", "keep_page_snapshot", "keep_temp_attachments", "allow_force_cleanup"},
    "markdown_export": {"enabled", "workspace_root", "source_markdown_dir", "preserve_source_tree",
                        "write_frontmatter", "atomic_publish"},
}


def settings_view(settings: Settings) -> dict:
    provider = provider_settings_view(settings)
    return {
        "pending_monitor": {
            "feishu_enabled": settings.feishu.enabled,
            "llm_enabled": settings.llm.enabled,
        },
        "summary_model": provider["local_llm"],
        "feishu": provider["feishu"],
        "data_cleanup": {name: getattr(settings.pending_cleanup, name) for name in sorted(SETTINGS_SECTIONS["pending_cleanup"])},
        "done_archive": {
            "enabled": settings.processing.enabled,
            "archive_dir": str(settings.archive_root),
            "compute_sha256": settings.storage.compute_sha256,
            "max_attachment_depth": settings.collector.max_attachment_depth,
        },
        "markdown": {name: getattr(settings.markdown_export, name) for name in sorted(SETTINGS_SECTIONS["markdown_export"])},
        "llm_wiki": {
            "workspace_root": str(settings.workspace_root),
            "source_dir": str(settings.markdown_root),
            "source_dir_exists": settings.markdown_root.exists(),
            "source_dir_writable": os.access(settings.markdown_root, os.W_OK),
            "write_frontmatter": settings.markdown_export.write_frontmatter,
            "atomic_publish": settings.markdown_export.atomic_publish,
        },
    }


def update_settings(config_path: Path | None, payload: dict[str, Any]) -> dict:
    if config_path is None:
        raise ValueError("configuration file is unavailable")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("configuration root must be a mapping")
    for section, allowed in SETTINGS_SECTIONS.items():
        incoming = payload.get(section)
        if not incoming:
            continue
        if not isinstance(incoming, dict):
            raise ValueError(f"{section} settings must be a mapping")
        unknown = set(incoming) - allowed
        if unknown:
            raise ValueError(f"unsupported {section} settings: {', '.join(sorted(unknown))}")
        current = raw.setdefault(section, {})
        current.update(incoming)
    candidate = config_path.with_suffix(config_path.suffix + ".candidate")
    candidate.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    try:
        updated = load_settings(candidate)
        candidate.replace(config_path)
    finally:
        candidate.unlink(missing_ok=True)
    return {**settings_view(updated), "restart_required": True}


# ---------------------------------------------------------------------------
# Maintenance (§9.5) — consolidated status + action dispatch
# ---------------------------------------------------------------------------

def maintenance_action(settings: Settings, config_path: Path | None, action: str, payload: dict | None = None) -> dict:
    from oa_knowledge.web.schedule_views import (
        notifications_test, schedule_control, trigger_schedule_run,
    )
    if action in {"run_hourly", "run_nightly"}:
        key = "hourly" if action == "run_hourly" else "nightly"
        return trigger_schedule_run(settings, key, config_path=config_path)
    if action == "schedule_control":
        return schedule_control(settings, (payload or {}).get("target"))
    if action == "retry_failed":
        from oa_knowledge.production_pipeline import ProductionQueue
        engine = create_db_engine(settings.database_path)
        try:
            return {"retried": ProductionQueue(engine).retry_failed()}
        finally:
            engine.dispose()
    if action == "notify_test":
        return notifications_test(settings)
    raise ValueError(f"unsupported maintenance action: {action}")


# ---------------------------------------------------------------------------
# Per-item retry (plan §7.2 / §8.2)
# ---------------------------------------------------------------------------

def retry_done_archive(settings: Settings, manifest_id: int) -> dict:
    return retry_manifest_failed_items(settings, manifest_id=manifest_id)


def rebuild_markdown_export(settings: Settings, export_id: int) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            export = session.get(MarkdownExport, export_id)
            if export is None or export.source_file_id is None:
                raise LookupError("markdown export not found")
            enqueued = enqueue_file(session, export.source_file_id)
            return {"export_id": export_id, "enqueued": enqueued}
    finally:
        engine.dispose()
