"""Scheduled sync + Feishu notification status and controls (plan-0805-02 §6).

These functions back the ``/api/schedule/*`` and ``/api/notifications/*``
endpoints, mirroring the ``oa schedule`` / ``oa notifications`` CLI commands so
the Web console can surface the "自动运行" (auto-run) panel. All reads are
database/local only; the trigger and test endpoints spawn the exact same
subprocess the systemd timers use, so no browser logic is duplicated here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oa_knowledge.config import validate_feishu_runtime_config
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import MarkdownTask, NotificationDelivery, OperationEvent, OperationJob, Run

REPO_ROOT = Path(__file__).resolve().parents[3]
STATIC_DIR = Path(__file__).with_name("static")

SCHEDULED_STAGES = ("scheduled_bootstrap", "scheduled_hourly", "scheduled_nightly")

# The five units the console manages (plan-0806-1 §6.2). Keys are stable and
# consumed directly by the WebUI service cards.
SYSTEMD_UNITS = {
    "web": "oaradar-web.service",
    "worker": "oaradar-worker.service",
    "markdown_worker": "oaradar-markdown-worker.service",
    "hourly_timer": "oaradar-hourly.timer",
    "nightly_timer": "oaradar-nightly.timer",
}
_TIMER_UNITS = {"hourly_timer", "nightly_timer"}


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


def _systemctl_show(unit: str) -> dict:
    """Return selected ``systemctl show`` properties for one unit, or ``{}``."""
    import shutil
    import subprocess

    if shutil.which("systemctl") is None:
        return {}
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "show", unit,
             "--property=LoadState,ActiveState,SubState,UnitFileState,"
             "ActiveEnterTimestamp,ExecMainStatus,Result,NextElapseUSec"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    props: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            props[key] = value
    return props


def _unit_detail(name: str) -> dict:
    """Per-unit status card data (plan-0806-1 §6.2)."""
    props = _systemctl_show(name)
    if not props:
        return {"installed": False, "enabled": False, "active": False,
                "last_started_at": None, "last_error": None, "next_run_at": None}
    installed = props.get("LoadState") == "loaded"
    enabled = props.get("UnitFileState") in ("enabled", "generated", "static")
    active = props.get("ActiveState") == "active"
    ts = props.get("ActiveEnterTimestamp") or ""
    last_started_at = ts if ts and ts != "n/a" else None
    result = props.get("Result", "success")
    last_error = None
    if result and result not in ("success", ""):
        status = props.get("ExecMainStatus")
        last_error = result if not status or status in ("0", "n/a") else f"{result} (exit {status})"
    next_run = None
    if name.split('.')[-1] == "timer":
        nxt = props.get("NextElapseUSec") or ""
        next_run = nxt if nxt and nxt != "n/a" else None
    return {"installed": installed, "enabled": enabled, "active": active,
            "last_started_at": last_started_at, "last_error": last_error,
            "next_run_at": next_run}


def _detect_systemd_services() -> dict:
    """Best-effort detection of the five managed systemd units (plan-0806-1 §6.2).

    Returns ``available=False`` whenever systemd is not in use on this host, so
    the endpoint stays informative without failing on a plain developer machine
    that runs scans manually. Each unit independently reports whether it is
    installed/enabled/active so the console never shows a single vague summary.
    """
    units = {key: _unit_detail(unit) for key, unit in SYSTEMD_UNITS.items()}
    available = any(u["installed"] for u in units.values())
    return {"available": available, "units": units}


def _system_info() -> dict:
    """Git commit + static-asset build time for the console header (plan §10)."""
    import shutil
    import subprocess

    git_commit = "unknown"
    if shutil.which("git") is not None:
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=5, cwd=str(REPO_ROOT),
            )
            if proc.returncode == 0:
                git_commit = proc.stdout.strip() or "unknown"
        except (OSError, subprocess.TimeoutExpired):
            pass
    build_time = None
    index = STATIC_DIR / "index.html"
    if index.exists():
        build_time = datetime.fromtimestamp(
            index.stat().st_mtime, tz=timezone.utc
        ).isoformat()
    return {"git_commit": git_commit, "build_time": build_time}


def _overall_status(systemd: dict, latest_job: dict | None, feishu_state: str) -> str:
    """Single banner value: 正常/未安装/已暂停/登录失效/配置异常 (plan-0806-1 §6.1)."""
    units = systemd["units"]
    if systemd["available"]:
        if not (units["worker"]["installed"] and units["hourly_timer"]["installed"]):
            return "未安装"
        if units["hourly_timer"]["installed"] and not units["hourly_timer"]["enabled"]:
            return "已暂停"
    if latest_job and latest_job["status"] == "auth_required":
        return "登录失效"
    if feishu_state in ("missing_webhook", "missing_secret", "invalid_webhook"):
        return "配置异常"
    return "正常"


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

            latest_job_row = session.scalar(
                select(OperationJob).where(OperationJob.job_type.in_(SCHEDULED_STAGES))
                .order_by(OperationJob.id.desc()).limit(1)
            )
            latest_job = {
                "job_id": latest_job_row.id,
                "status": latest_job_row.status,
                "last_error_code": latest_job_row.last_error_code,
            } if latest_job_row else None

            systemd = _detect_systemd_services()
    finally:
        engine.dispose()

    return {
        "recent_runs": recent,
        "last_scan_at": last_hourly["finished_at"] if last_hourly else None,
        "overall_status": _overall_status(systemd, latest_job, feishu_state),
        "services": systemd["units"],
        "system_info": _system_info(),
        "schedule_available": systemd["available"],
        "hourly_enabled": systemd["units"]["hourly_timer"]["enabled"],
        "next_run_at": systemd["units"]["hourly_timer"]["next_run_at"],
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
                "source_total": nightly_summary.get("done", {}).get("source_total", 0),
                "pages_scanned": nightly_summary.get("done", {}).get("pages_scanned", 0),
                "new_items": nightly_summary.get("done", {}).get("new_items", 0),
                "changed_items": nightly_summary.get("done", {}).get("changed_items", 0),
                "baseline_hashes": nightly_summary.get("done", {}).get("baseline_hashes", 0),
                "retry_items": nightly_summary.get("done", {}).get("retry_items", 0),
                "knowledge_tasks_enqueued": nightly_summary.get("done", {}).get(
                    "knowledge_tasks_enqueued",
                    nightly_summary.get("done", {}).get("markdown_tasks_enqueued", 0),
                ),
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
    """Enqueue a durable ``OperationJob`` for the worker to execute (plan-0806-1 §2.2).

    The Web layer no longer spawns an unmanaged background thread. The job is
    persisted in ``operation_jobs`` and picked up by the always-on ``oa worker``
    daemon, which runs the same scan the ``oa schedule <stage>`` CLI uses and
    records a ``Run`` row. We only return after the job row is confirmed in the
    database, so the caller never sees a false "已触发". ``stage`` is one of
    ``hourly`` / ``nightly``.
    """
    job_type = {"hourly": "scheduled_hourly", "nightly": "scheduled_nightly"}.get(stage)
    if job_type is None:
        raise ValueError(f"unsupported schedule stage: {stage}")
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            job_key = uuid4().hex
            idem = f"scheduled:{stage}:{job_key}"
            job = OperationJob(job_key=job_key, job_type=job_type, idempotency_key=idem)
            session.add(job)
            session.flush()
            job_id = job.id
            session.commit()
    finally:
        engine.dispose()
    return {"job_id": job_id, "status": "queued", "stage": stage}


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


def schedule_job_status(settings, job_id: int) -> dict:
    """Live progress of a manually-triggered scan job (plan-0806-1 §6.4).

    The WebUI polls this every couple of seconds after clicking "立即扫描" so
    the user sees a real stage/progress/counts instead of a vague "后台进程启动中".
    The run is linked best-effort by stage (the orchestration records a ``Run``
    row of the same stage), which is enough to surface the final counts.
    """
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            job = session.get(OperationJob, job_id)
            if job is None:
                return {"job_id": job_id, "found": False}
            event = session.scalar(
                select(OperationEvent).where(OperationEvent.job_id == job_id)
                .order_by(OperationEvent.sequence.desc()).limit(1)
            )
            run = session.scalar(
                select(Run).where(Run.stage == job.job_type)
                .order_by(Run.id.desc()).limit(1)
            )
            run_payload = None
            if run is not None:
                run_payload = {
                    "run_key": run.run_key,
                    "stage": run.stage,
                    "status": run.status,
                    "started_at": run.started_at.isoformat() if run.started_at else None,
                    "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                    "summary": json.loads(run.summary_json or "{}"),
                }
            return {
                "job_id": job_id,
                "found": True,
                "status": job.status,
                "stage": job.job_type,
                "progress_current": job.progress_current,
                "progress_total": job.progress_total,
                "last_error_code": job.last_error_code,
                "started_at": job.started_at.isoformat() if job.started_at else None,
                "finished_at": job.finished_at.isoformat() if job.finished_at else None,
                "current_event": event.event_type if event else None,
                "run": run_payload,
            }
    finally:
        engine.dispose()


# systemd control actions surfaced as buttons in the auto-run console (§6.3).
# Every action is best-effort: it shells out to `systemctl --user` and reports
# the result rather than failing the whole status call. Dangerous actions
# (install/enable/disable/restart) require a confirmed request from the UI.
_SCHEDULE_CONTROL_ACTIONS = {
    "enable": ["enable", "--now", "oaradar-hourly.timer", "oaradar-nightly.timer"],
    "disable": ["disable", "--now", "oaradar-hourly.timer", "oaradar-nightly.timer"],
    "restart_worker": ["restart", "oaradar-worker.service"],
    "relogin": ["restart", "oaradar-worker.service"],
    "install": ["enable", "--now",
                "oaradar-web.service", "oaradar-worker.service",
                "oaradar-markdown-worker.service",
                "oaradar-hourly.timer", "oaradar-nightly.timer"],
}


def schedule_control(settings, action: str) -> dict:
    """Run a systemd control action for the auto-run units (plan-0806-1 §6.3)."""
    import shutil
    import subprocess

    if action not in _SCHEDULE_CONTROL_ACTIONS:
        raise ValueError(f"unsupported schedule control action: {action}")
    if shutil.which("systemctl") is None:
        return {"ok": False, "action": action,
                "detail": "systemctl 不可用（非 systemd 环境）"}
    try:
        proc = subprocess.run(
            ["systemctl", "--user", *_SCHEDULE_CONTROL_ACTIONS[action]],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "action": action, "detail": "systemctl 超时"}
    ok = proc.returncode == 0
    detail = (proc.stdout or proc.stderr).strip().splitlines()[-1] if (proc.stdout or proc.stderr).strip() else ""
    return {"ok": ok, "action": action, "detail": detail,
            "returncode": proc.returncode}
