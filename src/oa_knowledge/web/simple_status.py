"""极简状态聚合接口（plan-0808 WebUI 简化，spec §4-§5）。

本模块只负责把现有事实表聚合成业务口径的结果，不读取任何 OA 正文、附件名、
凭据或模型输入输出。所有数字均来自数据库聚合，绝不硬编码验收值。

暴露：
- ``simple_status(settings) -> dict``：供 ``GET /api/simple-status`` 调用。
- ``_done_simple_status_map(session)``：供已办列表做服务端简化状态筛选复用。
- ``_classify_done_item`` / ``_SIMPLE_DONE_LABELS``：供列表单条计算复用。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings, validate_feishu_runtime_config
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import (
    ArchivedFile, ItemOccurrence, MarkdownExport, OAItem, OAManifestItem,
    SummaryJob, SummaryVersion,
)
from oa_knowledge.web.schedule_views import schedule_status
from oa_knowledge.web.status import dashboard_status

# 已办事项的简化状态及中文标签（spec §4.1 / §6.2）。
_SIMPLE_DONE_LABELS: dict[str, str] = {
    "waiting_download": "等待下载",
    "waiting_markdown": "等待 MD 化",
    "completed": "已完成",
    "attention": "需要处理",
    "excluded": "已按规则排除",
}

_ALLOWED_SIMPLE_STATES = tuple(_SIMPLE_DONE_LABELS)

# OA 后台活动文案（与 console_views._OA_JOB_ACTIVITY 保持一致口径，但此处独立避免循环导入）。
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

# 已办扫描频率固定从已部署计划事实翻译（spec §4.3 / §5）。
_DONE_SCAN_FREQUENCY_TEXT = "每小时 05 分检查"


def _classify_done_item(
    *,
    processing_status: str,
    has_success_item_index: bool,
    markdown_failed: bool,
) -> tuple[str, str | None]:
    """按 spec §4.1 优先级计算单个已办事项的主状态。

    返回 ``(state, attention_reason)``。``attention_reason`` 仅在需要处理时给出简短
    中文原因；其余状态为 ``None``。
    """
    if processing_status == "skipped":
        return "excluded", None
    if processing_status == "depth_limit_reached":
        # depth_limit_reached 永远属于“需要处理”，不得显示为已完成（spec §4.1）。
        return "attention", "容器层级超过上限，需人工确认"
    verified = processing_status in {"downloaded", "no_attachment"}
    if not verified:
        if processing_status in {"download_failed", "partial"}:
            return "attention", "原件下载失败"
        return "waiting_download", None
    # 原件已验证。
    if markdown_failed:
        return "attention", "Markdown 交付失败"
    if not has_success_item_index:
        return "waiting_markdown", None
    return "completed", None


def _done_simple_status_map(session: Session) -> dict[int, tuple[str, str, str | None]]:
    """为全部已办事项计算简化状态，返回 ``{manifest_id: (state, label, reason)}``。

    用于已办列表的服务端状态筛选，保证 total / 页数基于完整筛选结果（plan Task 2）。
    """
    manifests = session.execute(
        select(
            OAManifestItem.id,
            OAManifestItem.processing_status,
            OAManifestItem.oa_item_key,
        )
    ).all()

    indexed_keys = set(session.scalars(
        select(OAItem.oa_item_key)
        .join(MarkdownExport, MarkdownExport.oa_item_id == OAItem.id)
        .where(MarkdownExport.document_kind == "item_index", MarkdownExport.status == "success")
    ).all())
    failed_keys = set(session.scalars(
        select(OAItem.oa_item_key)
        .join(MarkdownExport, MarkdownExport.oa_item_id == OAItem.id)
        .where(MarkdownExport.document_kind == "item_index", MarkdownExport.status == "failed")
    ).all())

    result: dict[int, tuple[str, str, str | None]] = {}
    for mid, processing_status, key in manifests:
        state, reason = _classify_done_item(
            processing_status=processing_status,
            has_success_item_index=key in indexed_keys,
            markdown_failed=key in failed_keys,
        )
        result[mid] = (state, _SIMPLE_DONE_LABELS[state], reason)
    return result


def _done_summary(session: Session, schedule: dict) -> dict[str, Any]:
    """聚合已办知识库业务口径（spec §4.1-§4.2 / §5）。"""
    manifests = session.execute(
        select(OAManifestItem.id, OAManifestItem.processing_status)
    ).all()

    archive_complete = sum(1 for _, ps in manifests if ps in {"downloaded", "no_attachment"})
    no_attachment = sum(1 for _, ps in manifests if ps == "no_attachment")
    excluded = sum(1 for _, ps in manifests if ps == "skipped")
    waiting_download_items = sum(
        1 for _, ps in manifests if ps in {"discovered", "pending_download", "processing"}
    )
    download_issue_items = sum(
        1 for _, ps in manifests if ps in {"download_failed", "auth_required", "partial", "depth_limit_reached"}
    )

    status_map = _done_simple_status_map(session)

    state_counts: dict[str, int] = {state: 0 for state in _SIMPLE_DONE_LABELS}
    failed_items = 0
    review_items = 0
    for mid, _ in manifests:
        state, _label, reason = status_map[mid]
        state_counts[state] += 1
        if state == "attention":
            failed_items += 1

    oa_total = len(manifests)
    published_items = state_counts["completed"]
    markdown_ready_items = _markdown_ready_count(session)
    queued_items = state_counts["waiting_markdown"]
    running_items = 0

    attention_count = failed_items + review_items

    if oa_total == 0:
        headline = "已办知识库尚未同步任何事项。"
        status = "working"
    elif published_items == oa_total and attention_count == 0 and queued_items == 0:
        headline = f"已办知识库已完成：共 {oa_total} 项，已归档并发布 {published_items} 项。"
        status = "completed"
    else:
        headline = (
            f"已办知识库尚未完成：已同步 {oa_total} 项，{archive_complete} 项原件完整，"
            f"{published_items} 项已完成 Markdown 交付，"
            f"待下载 {waiting_download_items} 项、待 MD 化 {queued_items} 项。"
        )
        if attention_count > 0:
            headline += f"其中 {attention_count} 项需要处理。"
        status = "attention" if attention_count > 0 else "working"

    return {
        "status": status,
        "headline": headline,
        "oa_total": oa_total,
        "archive_complete": archive_complete,
        "waiting_download_items": waiting_download_items,
        "download_issue_items": download_issue_items,
        "excluded": excluded,
        "no_attachment": no_attachment,
        "markdown_ready_items": markdown_ready_items,
        "published_items": published_items,
        "queued_items": queued_items,
        "running_items": running_items,
        "failed_items": failed_items,
        "review_items": review_items,
        "last_scan_at": schedule.get("last_scan_at"),
    }


def _markdown_ready_count(session: Session) -> int:
    return session.scalar(
        select(func.count(func.distinct(OAItem.id)))
        .select_from(OAItem)
        .join(ArchivedFile, ArchivedFile.oa_item_id == OAItem.id)
        .join(MarkdownExport, MarkdownExport.source_file_id == ArchivedFile.id)
        .where(MarkdownExport.status == "success")
    ) or 0


def _pending_summary(session: Session, settings: Settings, schedule: dict) -> dict[str, Any]:
    """聚合待办提醒业务口径（spec §4.3 / §5）。"""
    notifications = schedule.get("notifications", {})
    counts = notifications.get("counts", {}) or {}
    feishu_sent = int(counts.get("sent", 0))
    feishu_failed = int(counts.get("failed", 0)) + int(counts.get("rejected", 0)) + int(counts.get("misconfigured", 0))
    feishu_unknown = int(counts.get("unknown", 0)) + int(counts.get("unknown_outcome", 0))
    last_feishu_success_at = notifications.get("last_success_at")

    model_success = session.scalar(
        select(func.count(func.distinct(SummaryVersion.logical_item_id)))
        .where(
            SummaryVersion.summary_kind == "pending",
            SummaryVersion.status == "current",
            SummaryVersion.model_name != "deterministic-fallback",
        )
    ) or 0
    model_fallback = session.scalar(
        select(func.count(func.distinct(SummaryVersion.logical_item_id)))
        .where(
            SummaryVersion.summary_kind == "pending",
            SummaryVersion.status == "current",
            SummaryVersion.model_name == "deterministic-fallback",
        )
    ) or 0
    failed_v = set(session.scalars(
        select(func.distinct(SummaryVersion.logical_item_id))
        .where(SummaryVersion.summary_kind == "pending", SummaryVersion.status == "failed")
    ).all())
    failed_j = set(session.scalars(
        select(func.distinct(SummaryJob.logical_item_id))
        .where(SummaryJob.summary_kind == "pending", SummaryJob.status == "failed")
    ).all())
    model_failed = len(failed_v | failed_j)

    oa_pending_count = session.scalar(
        select(func.count())
        .select_from(ItemOccurrence)
        .where(ItemOccurrence.channel == "pending", ItemOccurrence.occurrence_status == "active")
    ) or 0

    if feishu_failed > 0 or feishu_unknown > 0 or model_failed > 0:
        status = "attention"
    elif model_fallback > 0:
        status = "fallback_used"
    else:
        status = "normal"

    if status == "normal":
        headline = "待办提醒运行正常：飞书发送成功，本地模型正常输出。"
    elif status == "fallback_used":
        headline = f"待办提醒运行正常：飞书发送成功，模型曾使用 {model_fallback} 次保守兜底。"
    else:
        parts: list[str] = []
        if feishu_failed:
            parts.append(f"飞书失败 {feishu_failed} 条")
        if feishu_unknown:
            parts.append(f"飞书结果未知 {feishu_unknown} 条")
        if model_failed:
            parts.append(f"摘要失败 {model_failed} 条")
        detail = "，".join(parts) if parts else "存在未知异常"
        headline = f"待办提醒需要处理：{detail}。"

    return {
        "status": status,
        "headline": headline,
        "frequency_text": _DONE_SCAN_FREQUENCY_TEXT,
        "last_scan_at": schedule.get("last_scan_at"),
        "next_scan_at": schedule.get("next_run_at"),
        "oa_pending_count": oa_pending_count,
        "model_name": settings.llm.ollama_model,
        "model_success": int(model_success),
        "model_fallback": int(model_fallback),
        "model_failed": int(model_failed),
        "feishu_sent": feishu_sent,
        "feishu_failed": feishu_failed,
        "feishu_unknown": feishu_unknown,
        "last_feishu_success_at": last_feishu_success_at,
    }


def _oa_activity_card(base: dict, schedule: dict) -> dict[str, Any]:
    """隐私安全的 OA 后台活动卡，不含标题或凭据（spec §5 / §8）。"""
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


def _attention_list(done: dict, pending: dict) -> list[dict[str, Any]]:
    """聚合“需要处理”的条目，只列明确动作与入口（spec §6.1）。"""
    items: list[dict[str, Any]] = []
    done_attention = done.get("failed_items", 0) + done.get("review_items", 0)
    if done_attention > 0:
        items.append({
            "label": f"{done_attention} 项已办需要处理（下载 / 归类 / 复核）",
            "severity": "error",
            "jump": "done",
            "filter": "attention",
        })
    if pending.get("feishu_failed", 0) > 0:
        items.append({
            "label": f"{pending['feishu_failed']} 条飞书发送失败",
            "severity": "error",
            "jump": "settings",
        })
    if pending.get("feishu_unknown", 0) > 0:
        items.append({
            "label": f"{pending['feishu_unknown']} 条飞书发送结果未知",
            "severity": "error",
            "jump": "settings",
        })
    if pending.get("model_failed", 0) > 0:
        items.append({
            "label": f"{pending['model_failed']} 条待办摘要失败",
            "severity": "error",
            "jump": "settings",
        })
    return items


def _overall_status(done: dict, pending: dict, attention: list[dict]) -> str:
    """顶部总体结论状态（spec §6.1）。前端按该状态渲染中文横幅。"""
    if attention:
        return "attention"
    if done.get("status") == "completed" and pending.get("status") in {"normal", "fallback_used"}:
        return "normal"
    return "working"


def simple_status(settings: Settings) -> dict[str, Any]:
    """聚合极简业务状态。只读数据库与本地调度事实，不触碰任何 OA 内容。"""
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            schedule = schedule_status(settings)
            base = dashboard_status(settings)
            done = _done_summary(session, schedule)
            pending = _pending_summary(session, settings, schedule)
            oa_activity = _oa_activity_card(base, schedule)
            attention = _attention_list(done, pending)
            overall_status = _overall_status(done, pending, attention)
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "overall_status": overall_status,
                "done": done,
                "pending": pending,
                "oa_activity": oa_activity,
                "attention": attention,
            }
    finally:
        engine.dispose()
