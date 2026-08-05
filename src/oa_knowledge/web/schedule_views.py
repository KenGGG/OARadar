"""Scheduled sync + Feishu notification status and controls (plan-0805-02 §6).

These functions back the ``/api/schedule/*`` and ``/api/notifications/*``
endpoints, mirroring the ``oa schedule`` / ``oa notifications`` CLI commands so
the Web console can surface the "自动运行" (auto-run) panel. All reads are
database/local only; the trigger and test endpoints spawn the exact same
subprocess the systemd timers use, so no browser logic is duplicated here.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oa_knowledge.config import validate_feishu_runtime_config
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import MarkdownTask, NotificationDelivery, Run
from oa_knowledge.web.cli_runner import run_cli

SCHEDULED_STAGES = ("scheduled_bootstrap", "scheduled_hourly", "scheduled_nightly")
SCHEDULE_TIMER_UNIT = "oaradar-hourly.timer"
HOURLY_ON_CALENDAR = "TZ=Asia/Shanghai Mon..Fri *-*-* 09..17:05:00"


def _oa_login_state(recent: list[dict]) -> dict:
    """Best-effort OA login signal derived from the most recent scheduled run.

    A completed/partial scan implies the browser successfully authenticated; we
    report that plus the time it was last observed rather than inventing a live
    probe (the console is local-only and never holds OA credentials).
    """
    last = next((r for r in recent if r["stage"] in ("scheduled_hourly", "scheduled_nightly")), None)
    if last and last["status"] in {"completed", "partial"}:
        return {"status": "authenticated", "checked_at": last["finished_at"]}
    return {"status": "unknown", "checked_at": last["finished_at"] if last else None}


def _detect_systemd_schedule() -> dict:
    """Best-effort detection of the installed systemd timer state.

    Returns ``available=False`` (and ``None`` fields) whenever systemd is not in
    use on this host, so the endpoint stays informative without failing on a
    plain developer machine that runs scans manually.
    """
    result: dict = {"available": False, "hourly_enabled": None, "next_run_at": None}
    try:
        import shutil
        import subprocess

        if shutil.which("systemctl") is None:
            return result
        enabled = subprocess.run(
            ["systemctl", "--user", "is-enabled", SCHEDULE_TIMER_UNIT],
            capture_output=True, text=True, timeout=5,
        )
        result["hourly_enabled"] = enabled.returncode == 0
        timers = subprocess.run(
            ["systemctl", "--user", "list-timers", "--no-pager", SCHEDULE_TIMER_UNIT],
            capture_output=True, text=True, timeout=5,
        )
        if timers.returncode == 0:
            for line in timers.stdout.splitlines():
                if SCHEDULE_TIMER_UNIT in line:
                    parts = line.split()
                    result["next_run_at"] = parts[0] if parts else None
                    break
        result["available"] = True
    except Exception:
        result["available"] = False
    return result


def schedule_status(settings, limit: int = 10) -> dict:
    """Recent scheduled runs plus a convenience summary for the auto-run panel."""
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            rows = session.scalars(
                select(Run).where(Run.stage.in_(SCHEDULED_STAGES))
                .order_by(Run.id.desc()).limit(limit)
            ).all()
            recent = [{
                "run_key": r.run_key,
                "stage": r.stage,
                "status": r.status,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "summary": json.loads(r.summary_json or "{}"),
            } for r in rows]

            last_hourly = next((r for r in recent if r["stage"] == "scheduled_hourly"), None)
            last_nightly = next((r for r in recent if r["stage"] == "scheduled_nightly"), None)

            hourly_summary = last_hourly["summary"] if last_hourly else {}
            nightly_summary = last_nightly["summary"] if last_nightly else {}

            markdown_backlog = session.scalar(
                select(func.count()).select_from(MarkdownTask)
                .where(MarkdownTask.status.in_(("queued", "running")))
            ) or 0

            counts = dict(session.execute(
                select(NotificationDelivery.status, func.count())
                .group_by(NotificationDelivery.status)
            ).all())
            feishu_state = validate_feishu_runtime_config(settings)
            latest_sent = session.scalar(
                select(NotificationDelivery.sent_at)
                .where(NotificationDelivery.status == "sent")
                .order_by(NotificationDelivery.sent_at.desc()).limit(1)
            )
            latest_error = session.scalar(
                select(NotificationDelivery)
                .where(NotificationDelivery.error_code.is_not(None))
                .order_by(NotificationDelivery.updated_at.desc()).limit(1)
            )

            systemd = _detect_systemd_schedule()
    finally:
        engine.dispose()

    return {
        "recent_runs": recent,
        "last_scan_at": last_hourly["finished_at"] if last_hourly else None,
        "schedule_available": systemd["available"],
        "hourly_enabled": systemd["hourly_enabled"],
        "next_run_at": systemd["next_run_at"],
        "summary": {
            "pending_new": hourly_summary.get("pending", {}).get("created", 0),
            "pending_changed": hourly_summary.get("pending", {}).get("updated", 0),
            "done_new": hourly_summary.get("done", {}).get("new_items", 0),
            "markdown_backlog": markdown_backlog,
            "feishu": {
                "state": feishu_state,
                "sent": counts.get("sent", 0),
                "failed": counts.get("failed", 0) + counts.get("retry_wait", 0) + counts.get("unknown", 0),
            },
            "oa_login": _oa_login_state(recent),
            "nightly": {
                "last_at": last_nightly["finished_at"] if last_nightly else None,
                "markdown_tasks_enqueued": nightly_summary.get("done", {}).get("markdown_tasks_enqueued", 0),
                "download_jobs_enqueued": nightly_summary.get("done", {}).get("download_jobs_enqueued", 0),
            },
        },
        "notifications": {
            "feishu_state": feishu_state,
            "last_success_at": latest_sent.isoformat() if latest_sent else None,
            "last_error_code": latest_error.error_code if latest_error else None,
            "last_error_at": latest_error.updated_at.isoformat() if latest_error else None,
            "counts": counts,
        },
    }


def trigger_schedule_run(settings, stage: str, config_path: Path | None = None) -> dict:
    """Kick off an ``oa schedule <stage>`` scan in a background process.

    The spawned CLI records its own run into the ``runs`` table (which the
    status endpoint reads), so the web layer only has to launch it and return a
    handle. ``stage`` is one of ``hourly`` / ``nightly``.
    """
    if stage not in ("hourly", "nightly"):
        raise ValueError(f"unsupported schedule stage: {stage}")
    target = config_path

    def _run() -> None:
        run_cli([stage], config_path=target, timeout=None)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return {"triggered": True, "stage": stage, "mode": "background_process"}


def notifications_status(settings) -> dict:
    """Feishu delivery health: state, last success/error, status counts."""
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            latest_sent = session.scalar(
                select(NotificationDelivery.sent_at)
                .where(NotificationDelivery.status == "sent")
                .order_by(NotificationDelivery.sent_at.desc()).limit(1)
            )
            latest_error = session.scalar(
                select(NotificationDelivery)
                .where(NotificationDelivery.error_code.is_not(None))
                .order_by(NotificationDelivery.updated_at.desc()).limit(1)
            )
            counts = dict(session.execute(
                select(NotificationDelivery.status, func.count())
                .group_by(NotificationDelivery.status)
            ).all())
    finally:
        engine.dispose()
    state = validate_feishu_runtime_config(settings)
    return {
        "feishu_state": state,
        "last_success_at": latest_sent.isoformat() if latest_sent else None,
        "last_error_code": latest_error.error_code if latest_error else None,
        "last_error_at": latest_error.updated_at.isoformat() if latest_error else None,
        "counts": counts,
    }


def notifications_test(settings) -> dict:
    """Send the synthetic connectivity-test card (no real OA data)."""
    from oa_knowledge.notifications.feishu_service import FeishuService

    state = validate_feishu_runtime_config(settings)
    if state != "ready":
        return {
            "status": "misconfigured",
            "retryable": False,
            "error_code": state,
            "feishu_state": state,
        }
    result = FeishuService(settings).send_test()
    return {
        "status": result.status,
        "retryable": result.retryable,
        "error_code": result.error_code,
        "feishu_state": state,
    }


def notifications_retry(settings, delivery_id: int) -> dict:
    """Re-send a parked pending_summary delivery by id (manual retry)."""
    from oa_knowledge.notifications.feishu_service import retry_pending_summary_delivery

    engine = create_db_engine(settings.database_path)
    try:
        result = retry_pending_summary_delivery(engine, settings, delivery_id)
    finally:
        engine.dispose()
    return {
        "status": result.status,
        "retryable": result.retryable,
        "error_code": result.error_code,
    }
