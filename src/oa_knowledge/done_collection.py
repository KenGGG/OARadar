"""Durable, list-first Done collection with page and item checkpoints."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from oa_knowledge.collector import BrowserSession, DoneAdapter, LoginState
from oa_knowledge.config import Settings, load_settings
from oa_knowledge.constants import LEASE_TTL
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import OAManifestItem, OAManifestSync, OperationJob
from oa_knowledge.full_manifest import classify_manifest_rows, effective_exclusion_keywords, upsert_manifest_page
from oa_knowledge.resources import ResourceCoordinator

JOB_TYPES = ("done_inventory", "done_download", "done_login", "done_review")
ACTIVE = ("queued", "running", "paused", "auth_required")
SUGGESTIONS = (
    ("出差申请", "通常为个人事务"), ("请假申请", "通常为个人事务"),
    ("休假申请", "通常为个人事务"), ("报销单", "通常为个人费用记录"),
    ("用车申请", "可按资料留存需求决定"), ("考勤补签", "通常为个人事务"),
)


class CollectionAuthRequired(RuntimeError):
    pass


def _params(job: OperationJob) -> dict:
    return json.loads(job.parameters_json or "{}")


def _job_dict(job: OperationJob | None) -> dict | None:
    if job is None:
        return None
    params = _params(job)
    return {"id": job.id, "kind": job.job_type, "status": job.status,
            "current": job.progress_current or 0, "total": job.progress_total,
            "pages_scanned": params.get("next_page", 1) - 1,
            "total_pages": params.get("total_pages"),
            "pause_requested": bool(params.get("pause_requested")),
            "last_error": job.last_error_code, "active_item": params.get("active_item"),
            "failed": params.get("failed_count", 0),
            "deferred": len(set(params.get("deferred_keys", []))),
            "heartbeat_at": job.heartbeat_at.isoformat() if job.heartbeat_at else None}


def collection_status(settings: Settings) -> dict:
    from oa_knowledge.collector.windows_credentials import load_credential
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            jobs = {kind: session.scalar(select(OperationJob).where(OperationJob.job_type == kind)
                                        .order_by(OperationJob.id.desc()).limit(1)) for kind in JOB_TYPES}
            counts = dict(session.execute(select(OAManifestItem.processing_status, func.count())
                                          .group_by(OAManifestItem.processing_status)).all())
            suggestions = []
            titles = list(session.scalars(select(OAManifestItem.title)))
            for keyword, reason in SUGGESTIONS:
                hits = [title for title in titles if keyword in title]
                suggestions.append({"keyword": keyword, "reason": reason, "count": len(hits), "examples": hits[:3]})
            complete = jobs["done_inventory"] is not None and jobs["done_inventory"].status == "completed"
            missing = session.execute(select(
                func.count().filter(OAManifestItem.initiated_at.is_(None)),
                func.count().filter(OAManifestItem.completed_at.is_(None), OAManifestItem.completed_at_text.is_(None)),
                func.count().filter(OAManifestItem.sender.is_(None)),
            )).one()
        try:
            saved = load_credential(settings.browser.base_url) is not None
        except ValueError:
            saved = False
        return {"inventory": _job_dict(jobs["done_inventory"]), "download": _job_dict(jobs["done_download"]),
                "login": _job_dict(jobs["done_login"]), "inventory_complete": complete,
                "review": _job_dict(jobs["done_review"]),
                "counts": counts, "local_total": sum(counts.values()), "credentials_saved": saved,
                "connection_configured": ".invalid" not in settings.browser.base_url,
                "missing_fields": {"initiated_at": missing[0], "completed_at": missing[1], "sender": missing[2]},
                "suggestions": suggestions}
    finally:
        engine.dispose()


def start_collection(settings: Settings, kind: str) -> dict:
    if kind not in JOB_TYPES:
        raise ValueError("unsupported collection operation")
    if ".invalid" in settings.browser.base_url:
        raise ValueError("请在项目 .env 中填写 OA_BASE_URL")
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            # Serialize against any existing OA operation, including retired jobs.
            active = session.scalar(select(OperationJob).where(OperationJob.status.in_(("queued", "running")))
                                    .order_by(OperationJob.id.desc()).limit(1))
            if active:
                if active.job_type == kind:
                    return _job_dict(active)
                raise ValueError("已有 OA 任务正在运行，请先暂停或等待完成")
            previous = session.scalar(select(OperationJob).where(OperationJob.job_type == kind)
                                      .order_by(OperationJob.id.desc()).limit(1))
            if previous and previous.status in {"paused", "auth_required"}:
                raise ValueError("已有未完成任务，请点击继续以保留断点")
            previous_inventory = session.scalar(select(OperationJob).where(OperationJob.job_type == "done_inventory")
                                                .order_by(OperationJob.id.desc()).limit(1))
            if previous_inventory and _params(previous_inventory).get("source_origin", settings.browser.base_url) != settings.browser.base_url:
                raise ValueError("已有清单属于另一个 OA 来源，请使用独立数据目录")
            params = {"next_page": 1, "keys": [], "page_signatures": {}}
            if kind == "done_review":
                from oa_knowledge.title_screening import active_context
                context = active_context(session)
                if context is None:
                    raise ValueError("请先启用下载筛选方案")
                params = {"keys": [row.oa_item_key for row in session.scalars(select(OAManifestItem).order_by(OAManifestItem.list_ordinal))
                                    if context.decision(row)["action"] == "review"], "next_item": 0}
            if kind == "done_download":
                inventory = session.scalar(select(OperationJob).where(OperationJob.job_type == "done_inventory")
                                           .order_by(OperationJob.id.desc()).limit(1))
                if inventory is None or inventory.status != "completed":
                    raise ValueError("请先同步并核对完整已办清单")
                keys = _params(inventory)["keys"]
                rows = list(session.scalars(select(OAManifestItem).where(OAManifestItem.oa_item_key.in_(keys))))
                for row in rows:
                    if row.processing_status == "auth_required":
                        row.processing_status = "pending_download"
                classify_manifest_rows(session, rows, effective_exclusion_keywords(session), settings.data_root)
                params = {"keys": [row.oa_item_key for row in rows if row.processing_status not in {"downloaded", "no_attachment", "skipped"}],
                          "source_keys": keys, "failed_count": 0}
            params["source_origin"] = settings.browser.base_url
            job = OperationJob(job_key=f"{kind}-{uuid4().hex[:12]}", job_type=kind, status="queued",
                               idempotency_key=uuid4().hex, parameters_json=json.dumps(params),
                               progress_current=0, progress_total=len(params["keys"]) if kind in {"done_download", "done_review"} else None)
            session.add(job)
            from oa_knowledge.desktop_collection import reserve_desktop
            reserve_desktop(settings, job)
            session.commit()
            return _job_dict(job)
    finally:
        engine.dispose()


def control_collection(settings: Settings, job_id: int, action: str) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            job = session.get(OperationJob, job_id)
            if job is None or job.job_type not in JOB_TYPES:
                raise ValueError("collection task not found")
            params = _params(job)
            if params.get("source_origin", settings.browser.base_url) != settings.browser.base_url:
                raise ValueError("OA 来源已变化，不能继续原任务")
            if action == "pause" and job.status in {"queued", "running"}:
                params["pause_requested"] = True
                if job.status == "queued":
                    job.status = "paused"
                    job.lease_owner = None
                    job.lease_expires_at = None
            elif action == "resume" and job.status in {"paused", "auth_required", "failed"}:
                other = session.scalar(select(OperationJob.id).where(OperationJob.id != job.id,
                                       OperationJob.status.in_(("queued", "running"))).limit(1))
                if other:
                    raise ValueError("已有 OA 任务正在运行")
                params["pause_requested"] = False
                params.pop("timed_out_item", None)
                params["deferred_keys"] = []
                if job.job_type == "done_inventory" and job.status == "failed":
                    params.update(next_page=1, keys=[], page_signatures={})
                if job.job_type == "done_download":
                    for row in session.scalars(select(OAManifestItem).where(OAManifestItem.oa_item_key.in_(params["keys"]))):
                        if row.processing_status in {"auth_required", "processing"}:
                            row.processing_status = "pending_download"
                job.status = "queued"
                job.last_error_code = None
                job.finished_at = None
                job.lease_owner = None
                job.lease_expires_at = None
            else:
                raise ValueError("当前任务状态不支持此操作")
            job.parameters_json = json.dumps(params)
            if job.status == 'queued':
                from oa_knowledge.desktop_collection import reserve_desktop
                reserve_desktop(settings, job)
            session.commit()
            return _job_dict(job)
    finally:
        engine.dispose()


def pause_requested(engine, job_id: int) -> bool:
    with Session(engine) as session:
        job = session.get(OperationJob, job_id)
        return job is None or bool(_params(job).get("pause_requested"))


def _signature(items) -> str:
    return hashlib.sha256(json.dumps([(item.oa_item_key, item.title, item.sender,
                                      str(item.created_at), str(item.completed_at)) for item in items],
                                    ensure_ascii=False).encode()).hexdigest()


def _collection_settings(worker, job_id: int) -> Settings:
    settings = load_settings(worker.config_path) if worker.config_path else worker.settings
    with Session(worker.engine) as session:
        job = session.get(OperationJob, job_id)
        if _params(job).get("source_origin", settings.browser.base_url) != settings.browser.base_url:
            raise ValueError("OA source changed during collection")
    return settings


def run_inventory(worker, job_id: int) -> None:
    settings = _collection_settings(worker, job_id)
    engine = worker.engine
    coordinator = ResourceCoordinator(engine)
    lease = coordinator.acquire("oa_browser", worker.owner, ttl_seconds=1800, uses_local_gpu=False)
    if lease is None:
        return
    try:
        with Session(engine) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            job = session.get(OperationJob, job_id)
            params = _params(job)
            if params.get("pause_requested"):
                job.status = "paused"
                job.lease_owner = None
                job.lease_expires_at = None
                session.commit()
                return
            job.status = "running"
            job.started_at = job.started_at or datetime.now(timezone.utc)
            session.commit()
        with BrowserSession(settings) as browser:
            if browser.login_with_saved_credentials(30) != LoginState.AUTHENTICATED:
                _finish_collection(engine, job_id, "auth_required", "请检查已保存的账号，或打开浏览器完成登录")
                return
            adapter = DoneAdapter(browser.page, f"{browser.base_url}{settings.browser.done_list_path}")

            def read_page(number: int, offset: int = 0):
                nonlocal adapter
                for attempt in range(3):
                    try:
                        current = adapter.navigate_to_page(number, settings.collector.list_page_delay_seconds)
                        if adapter._current_page(current) != number:
                            raise ValueError("OA 返回的页码与请求不一致")
                        return current, adapter._discover_frame(current, 10000, number, offset)
                    except Exception:
                        if browser.login_state() != LoginState.AUTH_REQUIRED:
                            raise
                        if attempt == 2 or browser.login_with_saved_credentials(30) != LoginState.AUTHENTICATED:
                            raise CollectionAuthRequired("OA session expired") from None
                        adapter = DoneAdapter(browser.page, f"{browser.base_url}{settings.browser.done_list_path}")

            frame, first = read_page(1)
            total, pages = adapter._list_stats(frame)
            if total is None or pages is None:
                raise ValueError("OA 已办总数或页数无法读取")
            # A page-number checkpoint is safe only while the source boundary is stable.
            signatures = params.get("page_signatures", {})
            next_page = params.get("next_page", 1)
            changed = params.get("source_total", total) != total or params.get("total_pages", pages) != pages
            if signatures.get("1") and signatures["1"] != _signature(first):
                changed = True
            if next_page > 2 and not changed:
                _prior, prior_items = read_page(next_page - 1)
                changed = signatures.get(str(next_page - 1)) != _signature(prior_items)
            if changed:
                params.update(next_page=1, keys=[], page_signatures={})
            params.update(source_total=total, total_pages=pages)
            keys = set(params["keys"])
            # Bound each browser session; every page commits its checkpoint atomically.
            for page_number in range(params["next_page"], min(pages + 1, params["next_page"] + 10)):
                if pause_requested(engine, job_id):
                    _finish_collection(engine, job_id, "paused")
                    return
                frame, items = read_page(page_number, (page_number - 1) * len(first))
                if total and not items:
                    raise ValueError("OA 已办页面为空，清单同步未完成")
                keys.update(item.oa_item_key for item in items)
                params["keys"] = sorted(keys)
                params["next_page"] = page_number + 1
                params["page_signatures"][str(page_number)] = _signature(items)
                with Session(engine) as session:
                    session.execute(text("BEGIN IMMEDIATE"))
                    upsert_manifest_page(session, items)
                    job = session.get(OperationJob, job_id)
                    params["pause_requested"] = _params(job).get("pause_requested", False)
                    job.parameters_json = json.dumps(params)
                    job.progress_current = len(keys)
                    job.progress_total = total
                    job.heartbeat_at = datetime.now(timezone.utc)
                    job.lease_expires_at = job.heartbeat_at + LEASE_TTL
                    session.commit()
                coordinator.heartbeat(lease, worker.owner, ttl_seconds=1800)
            if params["next_page"] > pages:
                # Recheck source totals and head after the scan; drift never reports success.
                frame, final_first = read_page(1)
                final_totals = adapter._list_stats(frame)
                aligned = len(keys) == total and final_totals == (total, pages) and _signature(final_first) == params["page_signatures"].get("1")
                with Session(engine) as session:
                    session.add(OAManifestSync(oa_total_count=total, local_manifest_count=len(keys),
                                pages_scanned=pages, source_total_pages=pages,
                                status="manifest_complete" if aligned else "manifest_incomplete",
                                started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc)))
                    session.commit()
                _finish_collection(engine, job_id, "completed" if aligned else "failed",
                                   None if aligned else "清单总数不一致或扫描期间发生变化，请继续重新核对")
            else:
                _finish_collection(engine, job_id, "paused" if pause_requested(engine, job_id) else "queued")
    except CollectionAuthRequired:
        _finish_collection(engine, job_id, "auth_required", "OA 需要重新登录，检查保存的账号后点击继续")
    finally:
        coordinator.release(lease, worker.owner)


def _finish_collection(engine, job_id: int, status: str, error: str | None = None) -> None:
    with Session(engine) as session:
        session.execute(text("BEGIN IMMEDIATE"))
        job = session.get(OperationJob, job_id)
        if status == "queued" and _params(job).get("pause_requested"):
            status = "paused"
        job.status = status
        job.last_error_code = error
        if status != 'queued':
            job.lease_owner = None
            job.lease_expires_at = None
        if status in {"completed", "failed"}:
            job.finished_at = datetime.now(timezone.utc)
        session.commit()


def run_download(worker, job_id: int) -> None:
    from oa_knowledge.web.worker import _run_piped
    import sys
    settings = _collection_settings(worker, job_id)
    with Session(worker.engine) as session:
        session.execute(text("BEGIN IMMEDIATE"))
        job = session.get(OperationJob, job_id)
        params = _params(job)
        if params.get("pause_requested"):
            job.status = "paused"
            job.lease_owner = None
            job.lease_expires_at = None
            session.commit()
            return
        for row in session.scalars(select(OAManifestItem).where(OAManifestItem.oa_item_key.in_(params["keys"]))):
            if row.processing_status == "processing":
                row.processing_status = "pending_download"
        job.status = "running"
        job.started_at = job.started_at or datetime.now(timezone.utc)
        job.last_error_code = None
        session.commit()
    if not params["keys"]:
        _finish_collection(worker.engine, job_id, "completed")
        return
    coordinator = ResourceCoordinator(worker.engine)
    lease = coordinator.acquire("oa_browser", worker.owner, ttl_seconds=1800, uses_local_gpu=False)
    if lease is None:
        _finish_collection(worker.engine, job_id, "queued")
        return
    command = [sys.executable, "-m", "oa_knowledge.cli", "manifest", "download", "--max-items", str(len(params["keys"])), "--collection-job", str(job_id)]
    if worker.config_path:
        command.extend(["--config", str(worker.config_path)])

    def heartbeat():
        stop_child = False
        with Session(worker.engine) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            job = session.get(OperationJob, job_id)
            params = _params(job)
            rows = list(session.scalars(select(OAManifestItem).where(OAManifestItem.oa_item_key.in_(params["keys"]))))
            now = datetime.now(timezone.utc)
            for row in rows:
                if row.processing_status != "processing" or not row.last_retry_at:
                    continue
                started = row.last_retry_at.replace(tzinfo=timezone.utc) if row.last_retry_at.tzinfo is None else row.last_retry_at
                if (now - started).total_seconds() > settings.collector.attachment_total_timeout_seconds:
                    row.processing_status = "download_failed"
                    row.retry_count += 1
                    row.failure_stage = "attachment_timeout"
                    row.last_error = "单项下载超过总时限，已暂缓；其余事项继续，稍后可重试"
                    deferred = set(params.get("deferred_keys", [])); deferred.add(row.oa_item_key)
                    params["deferred_keys"] = sorted(deferred)
                    params["timed_out_item"] = row.oa_item_key
                    stop_child = True
            stop_child = stop_child or bool(params.get("pause_requested"))
            job.progress_current = sum(row.processing_status in {"downloaded", "no_attachment", "skipped"} for row in rows)
            params["failed_count"] = sum(row.processing_status in {"download_failed", "auth_required"} for row in rows)
            params["active_item"] = next((row.title for row in rows if row.processing_status == "processing"), None)
            job.parameters_json = json.dumps(params)
            job.heartbeat_at = datetime.now(timezone.utc)
            job.lease_expires_at = job.heartbeat_at + LEASE_TTL
            session.commit()
        coordinator.heartbeat(lease, worker.owner, ttl_seconds=1800)
        return False if stop_child else None

    try:
        code, _stdout, _stderr = _run_piped(command, Path.cwd(), heartbeat, poll_interval=2)
        heartbeat()
        with Session(worker.engine) as session:
            job = session.get(OperationJob, job_id)
            params = _params(job)
            current, total = job.progress_current, job.progress_total
        if params.get("pause_requested"):
            _finish_collection(worker.engine, job_id, "paused")
        elif params.get("timed_out_item"):
            with Session(worker.engine) as session:
                job = session.get(OperationJob, job_id)
                updated = _params(job); updated.pop("timed_out_item", None)
                job.parameters_json = json.dumps(updated)
                pending = session.scalar(select(func.count()).select_from(OAManifestItem).where(
                    OAManifestItem.oa_item_key.in_([key for key in updated["keys"] if key not in updated.get("deferred_keys", [])]),
                    OAManifestItem.processing_status.in_(("pending_download", "download_failed", "processing"))))
                session.commit()
            _finish_collection(worker.engine, job_id, "queued" if pending else "failed",
                               None if pending else "有超时事项已暂缓，点击继续可重试")
        elif code == 3:
            _finish_collection(worker.engine, job_id, "auth_required", "OA 需要重新登录，保存账号后点击继续")
        elif code == 0 and current == total:
            _finish_collection(worker.engine, job_id, "completed")
        else:
            _finish_collection(worker.engine, job_id, "failed", "仍有未下载或失败的事项，点击继续重试")
    finally:
        coordinator.release(lease, worker.owner)


def run_login(worker, job_id: int) -> None:
    settings = _collection_settings(worker, job_id)
    coordinator = ResourceCoordinator(worker.engine)
    lease = coordinator.acquire("oa_browser", worker.owner, ttl_seconds=180, uses_local_gpu=False)
    if lease is None:
        return
    with Session(worker.engine) as session:
        job = session.get(OperationJob, job_id)
        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        session.commit()
    try:
        with BrowserSession(settings, headed=True) as browser:
            browser.page.bring_to_front()
            state = browser.login_with_saved_credentials(300)
        _finish_collection(worker.engine, job_id, "completed" if state == LoginState.AUTHENTICATED else "auth_required",
                           None if state == LoginState.AUTHENTICATED else "登录未完成，请检查账号或验证码")
    finally:
        coordinator.release(lease, worker.owner)


def run_review(worker, job_id: int) -> None:
    """Checkpoint body previews without changing download recommendations."""
    from contextlib import ExitStack
    from urllib.parse import urlencode, urljoin
    from oa_knowledge.collector.detail import AuthRequiredError
    from oa_knowledge.collector.review import preview_detail
    from oa_knowledge.db.models import TitleReviewEvidence
    from oa_knowledge.title_screening import title_hash, cached_body_evidence, body_suggestion
    settings = _collection_settings(worker, job_id)
    coordinator = ResourceCoordinator(worker.engine)
    lease = coordinator.acquire("oa_browser", worker.owner, ttl_seconds=180, uses_local_gpu=False)
    if lease is None:
        _finish_collection(worker.engine, job_id, "queued")
        return
    try:
        with Session(worker.engine) as session:
            job = session.get(OperationJob, job_id)
            params = _params(job)
            keys = params["keys"]
            start = params.get("next_item", 0)
        with ExitStack() as stack:
            browser = adapter = None
            for index in range(start, min(start + 8, len(keys))):
                if pause_requested(worker.engine, job_id):
                    _finish_collection(worker.engine, job_id, "paused"); return
                with Session(worker.engine) as session:
                    row = session.scalar(select(OAManifestItem).where(OAManifestItem.oa_item_key == keys[index]))
                    if row is None:
                        raise ValueError("review_item_missing")
                    digest = title_hash(row.title)
                    existing = session.scalar(select(TitleReviewEvidence).where(
                        TitleReviewEvidence.oa_item_key == row.oa_item_key,
                        TitleReviewEvidence.title_sha256 == digest).order_by(TitleReviewEvidence.id.desc()).limit(1))
                    evidence = json.loads(existing.payload_json) if existing else cached_body_evidence(session, row, settings)
                    identity, page_number = row.workitem_id_text, row.list_page
                if evidence is None:
                    if browser is None:
                        browser = stack.enter_context(BrowserSession(settings))
                        if browser.login_with_saved_credentials(30) != LoginState.AUTHENTICATED:
                            raise AuthRequiredError("review_auth_required")
                        adapter = DoneAdapter(browser.page, f"{browser.base_url}{settings.browser.done_list_path}")
                        adapter.open_list()
                    adapter.navigate_to_page(page_number)
                    link = adapter.detail_link_for_item(identity)
                    url = urljoin(browser.base_url + "/", link) if link else f"{browser.base_url}{settings.browser.context_path}/collaboration/collaboration.do?" + urlencode(
                        {"method": "summary", "openFrom": "listDone", "affairId": identity, "showTab": "1"})
                    evidence = preview_detail(browser.context, url, browser.base_url)
                with Session(worker.engine) as session:
                    session.execute(text("BEGIN IMMEDIATE"))
                    if existing is None:
                        evidence["suggestion"] = body_suggestion(session, evidence)
                        session.add(TitleReviewEvidence(oa_item_key=keys[index], title_sha256=digest,
                                                       payload_json=json.dumps(evidence, ensure_ascii=False)))
                    job = session.get(OperationJob, job_id)
                    current = _params(job)
                    current["next_item"] = index + 1
                    job.parameters_json = json.dumps(current)
                    job.progress_current = index + 1
                    job.heartbeat_at = datetime.now(timezone.utc)
                    session.commit()
                coordinator.heartbeat(lease, worker.owner, ttl_seconds=180)
            next_index = min(start + 8, len(keys))
        _finish_collection(worker.engine, job_id, "completed" if next_index == len(keys) else "queued")
    except AuthRequiredError:
        _finish_collection(worker.engine, job_id, "auth_required", "正文预览需要重新登录，请登录后继续")
    except Exception:
        _finish_collection(worker.engine, job_id, "failed", "正文预览失败，保留原下载建议和断点，可重试")
    finally:
        coordinator.release(lease, worker.owner)
