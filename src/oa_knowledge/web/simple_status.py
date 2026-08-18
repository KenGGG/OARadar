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

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings, validate_feishu_runtime_config
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import (
    ArchivedFile, CuratedDecision, CuratedRun, ItemOccurrence, LogicalItem,
    MarkdownExport, OAItem, OAManifestItem, OnlineAuditItem, SummaryJob, SummaryVersion,
)
from oa_knowledge.web.schedule_views import schedule_status
from oa_knowledge.web.status import dashboard_status

# 已办事项的简化状态及中文标签（spec §4.1 / §6.2）。
_SIMPLE_DONE_LABELS: dict[str, str] = {
    "waiting_download": "等待下载",
    "waiting_markdown": "等待 MD 化",
    "waiting_classification": "等待归类",
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
    has_success_markdown: bool,
    curation: dict[str, Any] | None,
    depth_limited: bool,
) -> tuple[str, str | None]:
    """按 spec §4.1 优先级计算单个已办事项的主状态。

    返回 ``(state, attention_reason)``。``attention_reason`` 仅在需要处理时给出简短
    中文原因；其余状态为 ``None``。
    """
    if processing_status == "skipped":
        return "excluded", None
    if depth_limited:
        # depth_limit_reached 永远属于“需要处理”，不得显示为已完成（spec §4.1）。
        return "attention", "容器层级超过上限，需人工确认"
    verified = processing_status in {"downloaded", "no_attachment"}
    if not verified:
        if processing_status in {"download_failed", "partial"}:
            return "attention", "原件下载失败"
        return "waiting_download", None
    # 原件已验证。
    if not has_success_markdown:
        return "waiting_markdown", None
    # 已有有效 Source Markdown。
    if curation is None:
        return "waiting_classification", None
    if curation["status"] == "completed":
        if curation["decision_count"] > 0 and curation["all_published"]:
            return "completed", None
        return "attention", "部分决策尚未发布"
    if curation["status"] == "needs_review":
        return "attention", "归类待人工复核"
    if curation["status"] == "failed":
        return "attention", "归类失败"
    # queued / running 及其他：仍在归类队列中。
    return "waiting_classification", None


def _resolve_curation(session: Session, logical_item_id: int | None) -> dict[str, Any] | None:
    """取某个 logical item 最新一次 CuratedRun 的归类结论（spec §4.1）。"""
    if logical_item_id is None:
        return None
    curated = session.scalar(
        select(CuratedRun)
        .where(CuratedRun.logical_item_id == logical_item_id)
        .order_by(CuratedRun.id.desc())
        .limit(1)
    )
    if curated is None:
        return None
    decisions = list(session.scalars(
        select(CuratedDecision).where(CuratedDecision.curated_run_id == curated.id)
    ))
    n = len(decisions)
    published = sum(1 for d in decisions if d.status == "published")
    return {
        "status": curated.status,
        "decision_count": n,
        "all_published": n > 0 and n == published,
    }


def _is_depth_limited(session: Session, oa_item_key: str) -> bool:
    count = session.scalar(
        select(func.count())
        .select_from(OnlineAuditItem)
        .where(OnlineAuditItem.oa_item_key == oa_item_key, OnlineAuditItem.depth_limit_reached == True)  # noqa: E712
    )
    return bool(count)


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

    markdown_ready_ids = set(session.scalars(
        select(func.distinct(OAManifestItem.id))
        .select_from(OAManifestItem)
        .join(OAItem, OAItem.oa_item_key == OAManifestItem.oa_item_key)
        .join(ArchivedFile, ArchivedFile.oa_item_id == OAItem.id)
        .join(MarkdownExport, MarkdownExport.source_file_id == ArchivedFile.id)
        .where(MarkdownExport.status == "success")
    ).all())

    logical_rows = session.execute(
        select(OAItem.oa_item_key, OAItem.logical_item_id)
        .where(OAItem.logical_item_id.is_not(None))
    ).all()
    logical_by_key = {key: lid for key, lid in logical_rows}

    latest = (
        select(CuratedRun.logical_item_id.label("logical_item_id"), func.max(CuratedRun.id).label("max_id"))
        .group_by(CuratedRun.logical_item_id)
    ).subquery("latest")
    curated = (
        select(
            CuratedRun.logical_item_id.label("logical_item_id"),
            CuratedRun.status.label("status"),
            CuratedRun.id.label("run_id"),
        )
        .join(latest, (CuratedRun.logical_item_id == latest.c.logical_item_id) & (CuratedRun.id == latest.c.max_id))
    ).subquery("curated")
    dec = (
        select(
            CuratedDecision.curated_run_id.label("curated_run_id"),
            func.count().label("n"),
            func.sum(case((CuratedDecision.status == "published", 1), else_=0)).label("p"),
        )
        .group_by(CuratedDecision.curated_run_id)
    ).subquery("dec")
    curation_rows = session.execute(
        select(curated.c.logical_item_id, curated.c.status, dec.c.n, dec.c.p)
        .outerjoin(dec, dec.c.curated_run_id == curated.c.run_id)
    ).all()
    curation_by_logical = {
        lid: {
            "status": st,
            "decision_count": int(n or 0),
            "all_published": int(n or 0) > 0 and int(n or 0) == int(p or 0),
        }
        for lid, st, n, p in curation_rows
    }

    depth_rows = session.scalars(
        select(func.distinct(OnlineAuditItem.oa_item_key))
        .where(OnlineAuditItem.depth_limit_reached == True)  # noqa: E712
    ).all()
    depth_limited_keys = set(depth_rows)

    result: dict[int, tuple[str, str, str | None]] = {}
    for mid, processing_status, key in manifests:
        has_md = mid in markdown_ready_ids
        lid = logical_by_key.get(key)
        curation = curation_by_logical.get(lid) if lid is not None else None
        depth = key in depth_limited_keys
        state, reason = _classify_done_item(
            processing_status=processing_status,
            has_success_markdown=has_md,
            curation=curation,
            depth_limited=depth,
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

    status_map = _done_simple_status_map(session)

    state_counts: dict[str, int] = {state: 0 for state in _SIMPLE_DONE_LABELS}
    failed_items = 0
    review_items = 0
    for mid, _ in manifests:
        state, _label, reason = status_map[mid]
        state_counts[state] += 1
        if state == "attention":
            if reason == "归类待人工复核":
                review_items += 1
            else:
                failed_items += 1

    oa_total = len(manifests)
    published_items = state_counts["completed"]
    markdown_ready_items = _markdown_ready_count(session)
    queued_items = state_counts["waiting_markdown"] + state_counts["waiting_classification"]
    running_items = sum(
        1 for mid in status_map
        if status_map[mid][0] == "waiting_classification"
    )

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
            f"{published_items} 项完成最终归类，{queued_items} 项仍在排队。"
        )
        if attention_count > 0:
            headline += f"其中 {attention_count} 项需要处理。"
        status = "attention" if attention_count > 0 else "working"

    return {
        "status": status,
        "headline": headline,
        "oa_total": oa_total,
        "archive_complete": archive_complete,
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
        select(func.count(func.distinct(OAManifestItem.id)))
        .select_from(OAManifestItem)
        .join(OAItem, OAItem.oa_item_key == OAManifestItem.oa_item_key)
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
