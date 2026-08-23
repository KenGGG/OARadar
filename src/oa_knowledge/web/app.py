from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from oa_knowledge.config import Settings, load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.web.console_views import (
    build_dashboard,
    cleanup_eligible_pending,
    cleanup_pending,
    done_archives_list,
    markdown_outputs_list,
    pending_notification_detail,
    pending_notifications_list,
    rebuild_markdown_export,
    retry_done_archive,
    retry_pending_delivery,
    retry_pending_summary,
    sync_pending_occurrence,
    settings_view,
    update_settings,
)
from oa_knowledge.web.simple_status import simple_status, _ALLOWED_SIMPLE_STATES
from oa_knowledge.web.schedule_views import schedule_status
from oa_knowledge.web.status import dashboard_status


class AuthLoginRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)


class BulkPolicyRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    action: str = "metadata_only"
    scope: str = "title"


class ArchiveStartRequest(BaseModel):
    max_items: int = Field(default=10, ge=1, le=20)
    time_budget_seconds: int = Field(default=900, ge=60, le=1800)


class ReviewResolutionRequest(BaseModel):
    resolution: str


class BackfillStartRequest(BaseModel):
    from_date: str = Field(default="2019-01-01", pattern=r"^\d{4}-\d{2}-\d{2}$")
    to_date: str = Field(default="2026-01-01", pattern=r"^\d{4}-\d{2}-\d{2}$")
    chunk_size: int = Field(default=20, ge=1, le=20)
    time_budget_seconds: int = Field(default=1800, ge=60, le=1800)


class DataGovernancePlanRequest(BaseModel):
    categories: list[str] = Field(min_length=1, max_length=6)


class DataGovernanceActionRequest(BaseModel):
    confirmation: str | None = Field(default=None, max_length=100)


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
RETIRED_API_PREFIXES = (
    "/api/audits", "/api/lifecycle", "/api/knowledge", "/api/data-governance",
    "/api/maintenance", "/api/system/provider-settings", "/api/items", "/api/manifest",
    "/api/batches", "/api/backfill", "/api/notifications", "/api/jobs",
    "/api/events", "/api/discovery-jobs", "/api/policies", "/api/reviews",
)
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def create_web_app(settings: Settings, config_path: Path | None = None) -> FastAPI:
    """Build the read-only OA management console.

    Trust boundary: this app is only ever meant to be reached over a loopback
    address (``WebConfig.loopback_only`` enforces ``127.0.0.1`` / ``::1`` /
    ``localhost``) and ``local_security`` rejects any non-loopback ``Host`` header.
    Because the console is served over plain HTTP on loopback by design, the
    session/CSRF cookies intentionally keep ``secure=False`` — flipping them on
    would stop browsers from ever sending the cookies over ``http://127.0.0.1``.
    Operators who front the console with TLS should set ``secure=True`` /
    ``https_only=True`` here. Defense-in-depth for multi-user hosts is provided by
    the optional bootstrap-token gate (``web.require_auth``), below.
    """
    if not settings.database_path.exists():
        raise RuntimeError("database not initialized; run 'oa init' first")
    secret = _load_or_create_session_secret(settings.runtime_root)
    bootstrap_token = _load_or_create_bootstrap_token(settings.runtime_root)
    app = FastAPI(title="OA Knowledge Hub", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.bootstrap_token = bootstrap_token

    @app.middleware("http")
    async def local_security(request: Request, call_next):
        host = request.url.hostname
        if host not in {"127.0.0.1", "localhost", "::1", "testserver"}:
            return JSONResponse({"detail": "loopback host required"}, status_code=400)
        origin = request.headers.get("origin")
        if origin and origin not in _allowed_origins(settings):
            return JSONResponse({"detail": "cross-origin request rejected"}, status_code=403)
        if request.url.path.startswith(RETIRED_API_PREFIXES):
            return JSONResponse({"detail": "API route not found"}, status_code=404)
        # Optional bootstrap-token gate: when enabled, every /api/* call (except the
        # two auth endpoints themselves) must carry the session established by
        # POST /api/auth/login. The frontend root and static assets stay open so the
        # login page can load.
        if settings.web.require_auth and request.url.path.startswith("/api/") and request.url.path not in {
            "/api/auth/login", "/api/auth/status",
        }:
            if not request.session.get("authenticated"):
                return JSONResponse({"detail": "authentication required"}, status_code=401)
        if request.url.path.startswith("/api/") and request.method not in SAFE_METHODS:
            csrf_cookie = request.cookies.get("oa_csrf")
            csrf_header = request.headers.get("x-csrf-token")
            if not csrf_cookie or not secrets.compare_digest(csrf_cookie, csrf_header or ""):
                return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        if "oa_csrf" not in request.cookies:
            response.set_cookie(
                "oa_csrf", secrets.token_urlsafe(32), httponly=False, samesite="strict", secure=False, max_age=8 * 60 * 60
            )
        return response

    @app.get("/api/status")
    def get_status() -> dict:
        try:
            return dashboard_status(settings)
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail="local status unavailable") from exc

    @app.get("/api/auth/status")
    def auth_status(request: Request) -> dict:
        return {
            "required": settings.web.require_auth,
            "authenticated": bool(request.session.get("authenticated")),
        }

    @app.post("/api/auth/login", status_code=204)
    def auth_login(request: Request, payload: AuthLoginRequest) -> None:
        expected = app.state.bootstrap_token
        # Constant-time comparison so a wrong token does not leak timing. The
        # token is owner-only on disk (mode 0600); a browser can only obtain it by
        # reading the launcher's terminal output once.
        if not secrets.compare_digest(payload.token, expected):
            raise HTTPException(status_code=401, detail="invalid bootstrap token")
        request.session["authenticated"] = True

    @app.get("/api/audits/online")
    def get_online_audit(
        item_page: int = Query(1, ge=1),
        item_page_size: int = Query(50, ge=10, le=200),
    ) -> dict:
        return audit_view(settings, item_page=item_page, item_page_size=item_page_size)

    @app.post("/api/audits/online", status_code=202)
    def post_online_audit() -> dict:
        return start_audit(settings)

    @app.post("/api/audits/online/{run_id}/pause")
    def post_online_audit_pause(run_id: int) -> dict:
        try:
            return pause_audit(settings, run_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/audits/online/{run_id}/resume")
    def post_online_audit_resume(run_id: int) -> dict:
        try:
            return resume_audit(settings, run_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/audits/markdown/pause")
    def post_markdown_pause() -> dict:
        from oa_knowledge.markdown_queue import pause_queue
        return pause_queue(settings)

    @app.post("/api/audits/markdown/resume")
    def post_markdown_resume() -> dict:
        from oa_knowledge.markdown_queue import resume_queue
        return resume_queue(settings)

    @app.post("/api/audits/markdown/retry-failed")
    def post_markdown_retry() -> dict:
        from oa_knowledge.markdown_queue import retry_failed
        return retry_failed(settings)

    @app.post("/api/audits/pdf-mineru", status_code=202)
    def post_pdf_mineru_start() -> dict:
        from oa_knowledge.markdown_queue import start_pdf_mineru_campaign
        return start_pdf_mineru_campaign(settings)

    @app.post("/api/audits/pdf-mineru/pause")
    def post_pdf_mineru_pause() -> dict:
        from oa_knowledge.markdown_queue import set_pdf_mineru_paused
        return set_pdf_mineru_paused(settings, True)

    @app.post("/api/audits/pdf-mineru/resume")
    def post_pdf_mineru_resume() -> dict:
        from oa_knowledge.markdown_queue import set_pdf_mineru_paused
        return set_pdf_mineru_paused(settings, False)

    @app.post("/api/audits/pdf-mineru/retry-failed")
    def post_pdf_mineru_retry() -> dict:
        from oa_knowledge.markdown_queue import retry_pdf_mineru_failed
        return retry_pdf_mineru_failed(settings)

    @app.get("/api/audits/archive-dates")
    def get_archive_dates() -> dict:
        return archive_date_status(settings)

    @app.post("/api/audits/archive-dates", status_code=202)
    def post_archive_dates() -> dict:
        return start_archive_date_job(settings)

    @app.post("/api/audits/archive-dates/pause")
    def post_archive_dates_pause() -> dict:
        return set_archive_date_job_paused(settings, True)

    @app.post("/api/audits/archive-dates/resume")
    def post_archive_dates_resume() -> dict:
        return set_archive_date_job_paused(settings, False)

    @app.get("/api/lifecycle/pending")
    def get_lifecycle_pending() -> dict:
        return lifecycle_pending_list(settings)

    @app.get("/api/lifecycle/pending/{occurrence_id}")
    def get_lifecycle_pending_detail(occurrence_id: int) -> dict:
        try:
            return lifecycle_pending_detail(settings, occurrence_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/lifecycle/done")
    def get_lifecycle_done(
        page: int = Query(1, ge=1),
        page_size: int = Query(100, ge=1, le=200),
        query: str | None = Query(None),
    ) -> dict:
        return lifecycle_done_list(settings, page=page, page_size=page_size, query=query)

    @app.get("/api/lifecycle/knowledge")
    def get_lifecycle_knowledge() -> dict:
        return lifecycle_knowledge_list(settings)

    @app.get("/api/lifecycle/knowledge/{document_id}")
    def get_lifecycle_knowledge_detail(document_id: int) -> dict:
        try:
            return lifecycle_knowledge_detail(settings, document_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/lifecycle/system")
    def get_lifecycle_system() -> dict:
        return lifecycle_system_view(settings)

    @app.get("/api/lifecycle/processing-center")
    def get_lifecycle_processing_center() -> dict:
        return lifecycle_processing_center(settings)

    @app.post("/api/knowledge/rebuild", status_code=202)
    def post_knowledge_rebuild() -> dict:
        engine = create_db_engine(settings.database_path)
        try:
            result = ProductionQueue(engine).start_historical_rebuild()
            return {"status": "running", **result}
        finally:
            engine.dispose()

    @app.post("/api/knowledge/rebuild/pause")
    def post_knowledge_rebuild_pause() -> dict:
        engine = create_db_engine(settings.database_path)
        try:
            ProductionQueue(engine).set_historical_paused(True)
            return {"status": "paused"}
        finally:
            engine.dispose()

    @app.post("/api/knowledge/rebuild/resume")
    def post_knowledge_rebuild_resume() -> dict:
        engine = create_db_engine(settings.database_path)
        try:
            ProductionQueue(engine).set_historical_paused(False)
            return {"status": "running"}
        finally:
            engine.dispose()

    @app.get("/api/data-governance")
    def get_data_governance() -> dict:
        return data_governance_view(settings)

    @app.post("/api/data-governance/plans", status_code=202)
    def post_data_governance_plan(payload: DataGovernancePlanRequest) -> dict:
        try:
            return enqueue_data_governance_plan(settings, set(payload.categories))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/data-governance/integrity-audits", status_code=202)
    def post_integrity_audit() -> dict:
        return enqueue_integrity_audit(settings)

    @app.post("/api/data-governance/runs/{run_id}/{action}", status_code=202)
    def post_data_governance_action(
        run_id: int, action: str, payload: DataGovernanceActionRequest,
    ) -> dict:
        try:
            return enqueue_data_governance_action(
                settings, run_id, action, confirmation=payload.confirmation,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ---- Product-aligned console routes (plan-0807-1 §3-§10) ----

    @app.get("/api/dashboard")
    def get_dashboard() -> dict:
        try:
            return build_dashboard(settings)
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail=f"dashboard unavailable: {exc}") from exc

    @app.get("/api/simple-status")
    def get_simple_status() -> dict:
        try:
            return simple_status(settings)
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=f"simple status unavailable: {exc}") from exc

    @app.get("/api/pending-notifications")
    def get_pending_notifications(
        filter: str | None = Query(None, description="processing|summary_failed|feishu_failed|awaiting_cleanup|cleanup_failed|recent_success"),
    ) -> dict:
        return pending_notifications_list(settings, filter_kind=filter)

    @app.get("/api/pending-notifications/{occurrence_id}")
    def get_pending_notification_detail(occurrence_id: int) -> dict:
        try:
            return pending_notification_detail(settings, occurrence_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/pending-notifications/{occurrence_id}/retry-summary", status_code=202)
    def post_pending_retry_summary(occurrence_id: int) -> dict:
        try:
            return retry_pending_summary(settings, occurrence_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/pending-notifications/{occurrence_id}/retry-delivery", status_code=202)
    def post_pending_retry_delivery(occurrence_id: int) -> dict:
        try:
            return retry_pending_delivery(settings, occurrence_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/pending-notifications/{occurrence_id}/cleanup", status_code=202)
    def post_pending_cleanup(occurrence_id: int, payload: dict | None = None) -> dict:
        force = bool((payload or {}).get("force"))
        try:
            return cleanup_pending(settings, occurrence_id, force=force)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/pending-notifications/cleanup-eligible", status_code=202)
    def post_pending_cleanup_eligible() -> dict:
        try:
            return cleanup_eligible_pending(settings)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/pending-notifications/{occurrence_id}/sync-oa", status_code=202)
    def post_pending_sync_oa(occurrence_id: int) -> dict:
        try:
            return sync_pending_occurrence(settings, occurrence_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/done-archives")
    def get_done_archives(
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
        query: str | None = Query(None),
        archive_status: str | None = Query(None),
        markdown_status: str | None = Query(None),
        handoff_status: str | None = Query(None),
        simple_status: str | None = Query(None),
    ) -> dict:
        if simple_status is not None and simple_status not in _ALLOWED_SIMPLE_STATES:
            raise HTTPException(status_code=422, detail=f"unsupported simple_status: {simple_status}")
        return done_archives_list(
            settings, page=page, page_size=page_size, query=query,
            archive_status=archive_status, markdown_status=markdown_status, handoff_status=handoff_status,
            simple_status=simple_status,
        )

    @app.get("/api/markdown-outputs")
    def get_markdown_outputs(
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
    ) -> dict:
        return markdown_outputs_list(settings, page=page, page_size=page_size)

    @app.post("/api/done-archives/{manifest_id}/retry-archive", status_code=202)
    def post_done_archive_retry(manifest_id: int) -> dict:
        try:
            return retry_done_archive(settings, manifest_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/markdown-outputs/{export_id}/rebuild", status_code=202)
    def post_markdown_rebuild(export_id: int) -> dict:
        try:
            return rebuild_markdown_export(settings, export_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/settings")
    def get_settings() -> dict:
        return settings_view(settings)

    @app.patch("/api/settings")
    def patch_settings(payload: dict) -> dict:
        try:
            return update_settings(config_path, payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/maintenance")
    def get_maintenance_view(target_items: int = Query(500, ge=1, le=100000)) -> dict:
        return maintenance_status(settings, target_items)

    @app.post("/api/maintenance/actions")
    def post_maintenance_action(payload: dict) -> dict:
        action = payload.get("action") if isinstance(payload, dict) else None
        try:
            return maintenance_action(settings, config_path, action, payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/system/provider-settings")
    def get_provider_settings() -> dict:
        current = load_settings(config_path) if config_path else settings
        return provider_settings_view(current)

    @app.patch("/api/system/provider-settings")
    def patch_provider_settings(payload: dict) -> dict:
        try:
            return update_provider_settings(config_path, payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/items")
    def get_items(
        page: int = Query(1, ge=1, description="Page number"),
        page_size: int = Query(20, ge=1, le=100, description="Items per page"),
        pipeline_status: str | None = Query(None, description="Filter by pipeline status"),
        source_channel: str | None = Query(None, description="Filter by source channel"),
        search: str | None = Query(None, description="Search title, sender, or item key"),
    ) -> dict:
        return list_items(settings, page=page, page_size=page_size, pipeline_status_filter=pipeline_status, source_channel=source_channel, search=search)

    @app.get("/api/items/{item_id}")
    def get_item_detail(item_id: int) -> dict:
        try:
            return item_detail(settings, item_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/manifest/items")
    def get_manifest_items(
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
        status: str | None = Query(None),
        statuses: str | None = Query(None),
        search: str | None = Query(None),
        sender: str | None = Query(None),
        keyword: str | None = Query(None),
        attachment_filter: str | None = Query(None),
        start_date: str | None = Query(None),
        end_date: str | None = Query(None),
        sort: str = Query("completed_at"),
        direction: str = Query("desc"),
    ) -> dict:
        return list_manifest_items(
            settings, page=page, page_size=page_size, status=status, statuses=statuses,
            search=search, sender=sender, keyword=keyword, attachment_filter=attachment_filter,
            start_date=start_date, end_date=end_date, sort=sort, direction=direction,
        )

    @app.get("/api/manifest/items/{manifest_id}")
    def get_manifest_item_detail(manifest_id: int) -> dict:
        try:
            return manifest_item_detail(settings, manifest_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/manifest/retry", status_code=202)
    def post_manifest_retry(
        search: str | None = Query(None),
        statuses: str | None = Query(None),
        sender: str | None = Query(None),
        keyword: str | None = Query(None),
        manifest_id: int | None = Query(None),
    ) -> dict:
        return retry_manifest_failed_items(settings, search=search, statuses=statuses, sender=sender, keyword=keyword, manifest_id=manifest_id)

    @app.post("/api/manifest/recheck-no-attachment", status_code=202)
    def post_manifest_recheck_no_attachment(
        search: str | None = Query(None),
        sender: str | None = Query(None),
        keyword: str | None = Query(None),
        manifest_id: int | None = Query(None),
    ) -> dict:
        return recheck_manifest_no_attachment(settings, search=search, sender=sender, keyword=keyword, manifest_id=manifest_id)

    @app.post("/api/manifest/audit-all", status_code=202)
    def post_manifest_audit_all() -> dict:
        return audit_all_manifest_items(settings)

    @app.post("/api/manifest/items/{manifest_id}/manual-review")
    def post_manifest_manual_review(manifest_id: int) -> dict:
        try:
            return mark_manifest_manual_review(settings, manifest_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/manifest/files/{file_id}/open")
    def post_manifest_file_open(file_id: int, target: str = Query("file")) -> dict:
        try:
            return open_archived_file(settings, file_id, target)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/batches")
    def get_batches() -> dict:
        return list_batches(settings)

    @app.post("/api/backfill/start", status_code=202)
    def post_backfill_start(payload: BackfillStartRequest) -> dict:
        try:
            return start_backfill_campaign(
                settings, payload.from_date, payload.to_date,
                payload.chunk_size, payload.time_budget_seconds,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/backfill/status")
    def get_backfill_status() -> dict | None:
        return latest_backfill_campaign(settings)

    @app.get("/api/manifest/status")
    def get_manifest_status() -> dict:
        return full_manifest_status(settings)

    @app.post("/api/manifest/start", status_code=202)
    def post_manifest_start() -> dict:
        return start_full_manifest_job(settings)

    @app.post("/api/manifest/refresh-incremental", status_code=202)
    def post_manifest_refresh_incremental() -> dict:
        return start_done_incremental_job(settings)

    @app.get("/api/manifest/report")
    def get_manifest_report() -> FileResponse:
        path = full_manifest_report_path(settings)
        return FileResponse(path, media_type="text/csv; charset=utf-8", filename="oa_manifest.csv")

    # ---- 2B-4: Scheduled sync + Feishu notification status (plan-0805-02 §6) ----

    @app.get("/api/schedule/status")
    def get_schedule_status(limit: int = Query(10, ge=1, le=100)) -> dict:
        try:
            return schedule_status(settings, limit=limit)
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=f"schedule status unavailable: {exc}") from exc

    @app.post("/api/schedule/hourly", status_code=202)
    def post_schedule_hourly() -> dict:
        try:
            return trigger_schedule_run(settings, "hourly", config_path=config_path)
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail="could not start hourly scan") from exc

    @app.post("/api/schedule/nightly", status_code=202)
    def post_schedule_nightly() -> dict:
        try:
            return trigger_schedule_run(settings, "nightly", config_path=config_path)
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail="could not start nightly sync") from exc

    @app.get("/api/schedule/job/{job_id}")
    def get_schedule_job(job_id: int) -> dict:
        try:
            return schedule_job_status(settings, job_id)
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail="schedule job status unavailable") from exc

    @app.post("/api/schedule/control")
    def post_schedule_control(payload: dict) -> dict:
        action = payload.get("action") if isinstance(payload, dict) else None
        try:
            result = schedule_control(settings, action)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail="schedule control failed") from exc
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("detail", "schedule control failed"))
        return result

    @app.get("/api/notifications/status")
    def get_notifications_status() -> dict:
        return notifications_status(settings)

    @app.post("/api/notifications/test", status_code=202)
    def post_notifications_test() -> dict:
        result = notifications_test(settings)
        if result.get("status") not in {"sent"}:
            raise HTTPException(status_code=409, detail=result.get("error_code", "feishu_test_failed"))
        return result

    @app.post("/api/notifications/{delivery_id}/retry", status_code=202)
    def post_notifications_retry(delivery_id: int) -> dict:
        result = notifications_retry(settings, delivery_id)
        if result.get("status") not in {"sent"}:
            raise HTTPException(status_code=409, detail=result.get("error_code", "feishu_retry_failed"))
        return result

    @app.post("/api/batches/{batch_id}/start")
    def post_batch_start(
        batch_id: int, payload: ArchiveStartRequest,
    ) -> dict:
        try:
            job = start_archive_job(settings, batch_id, payload.max_items, payload.time_budget_seconds)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return job

    @app.post("/api/batches/{batch_id}/pause")
    def post_batch_pause(batch_id: int) -> dict:
        try:
            return pause_archive_batch(settings, batch_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # ---- 2B-2: Batch control routes ----

    @app.post("/api/batches/{batch_id}/freeze")
    def post_batch_freeze(batch_id: int) -> dict:
        try:
            return freeze_archive_batch(settings, batch_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/batches/{batch_id}/resume")
    def post_batch_resume(batch_id: int) -> dict:
        try:
            return resume_archive_batch(settings, batch_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/batches/{batch_id}/cancel")
    def post_batch_cancel(batch_id: int) -> dict:
        try:
            return cancel_archive_batch(settings, batch_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/batches/{batch_id}/retry")
    def post_batch_retry(batch_id: int) -> dict:
        try:
            return retry_batch_items(settings, batch_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/batches/{batch_id}/preview")
    def get_batch_preview(batch_id: int) -> dict:
        try:
            return batch_items_preview(settings, batch_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/jobs/{job_id}")
    def get_job_progress(job_id: int) -> dict | None:
        result = job_progress(settings, job_id)
        if result is None:
            raise HTTPException(status_code=404, detail="job not found")
        return result

    @app.get("/api/events")
    def get_events(
        job_id: int | None = Query(None, description="Filter events for a specific job"),
    ) -> list[dict]:
        return list_events(settings, since_job_id=job_id)

    @app.get("/api/events/stream")
    async def event_stream(
        job_id: int | None = Query(None, description="Stream events for a specific job"),
    ):
        """Simple SSE stream that polls the database every 2 seconds."""
        last_id: int | None = None

        async def _generate():
            nonlocal last_id
            while True:
                try:
                    events = list_events(settings, since_job_id=job_id)
                    if events:
                        newest = max(e["id"] for e in events)
                        if last_id is None or newest != last_id:
                            for evt in events:
                                yield f"id: {evt['id']}\ndata: {json.dumps(evt, ensure_ascii=False)}\n\n"
                            last_id = newest
                except Exception:
                    pass
                await asyncio.sleep(2)

        return StreamingResponse(_generate(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        })

    @app.head("/api/events/stream")
    async def event_stream_head(
        job_id: int | None = Query(None, description="Stream events for a specific job"),
    ):
        """Head probe for SSE endpoint (returns 200 without hanging)."""
        return JSONResponse({"detail": "SSE endpoint; use GET for streaming"})

    # ---- 2B-1: Discovery jobs ----

    @app.get("/api/discovery-jobs")
    def get_discovery_jobs() -> list[dict]:
        return list_discovery_jobs(settings)

    @app.post("/api/discovery-jobs")
    def post_discovery_job(
        request: Request,
        source_channel: str = Query("done", description="Source channel to discover"),
        days_back: int = Query(30, ge=1, le=365, description="Days to look back"),
    ) -> dict:
        try:
            return create_discovery_job(settings, source_channel=source_channel, days_back=days_back)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # ---- 2B-1: Exclusion policies ----

    @app.get("/api/policies")
    def get_policies() -> list[dict]:
        return list_policies(settings)

    @app.post("/api/policies")
    def post_policy(
        request: Request,
        name: str = Query(..., description="Policy name"),
        pattern: str = Query(..., description="Search pattern"),
        action: str = Query("metadata_only", description="Action: skip or metadata_only"),
        scope: str = Query("title", description="Scope: title, sender, category, or full"),
        description: str | None = Query(None, description="Policy description"),
    ) -> dict:
        try:
            return create_policy(settings, name=name, pattern=pattern, action=action, scope=scope, description=description)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/policies/bulk")
    def post_policies_bulk(payload: BulkPolicyRequest) -> dict:
        try:
            return create_policies_bulk(
                settings, text=payload.text, action=payload.action, scope=payload.scope,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/policies/{policy_id}")
    def delete_policy_route(policy_id: int) -> dict:
        result = delete_policy(settings, policy_id=policy_id)
        if result is None:
            raise HTTPException(status_code=404, detail="policy not found")
        return result

    @app.get("/api/policies/preview")
    def preview_hits(
        pattern: str = Query(..., description="Pattern to preview"),
        scope: str = Query("title", description="Scope: title, sender, or full"),
        limit: int = Query(50, ge=1, le=200, description="Max samples to return"),
    ) -> dict:
        try:
            return preview_policy_hits(settings, pattern=pattern, scope=scope, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ---- 2B-3: Review and local maintenance ----

    @app.get("/api/reviews")
    def get_reviews(
        status: str = Query("pending"),
        kind: str | None = Query(None),
    ) -> list[dict]:
        try:
            return list_reviews(settings, status, kind)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/reviews/{review_id}/resolve")
    def post_review_resolution(review_id: int, payload: ReviewResolutionRequest) -> dict:
        try:
            return resolve_review(settings, review_id, payload.resolution)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/reviews/{review_id}/retry-source", status_code=202)
    def post_source_review_retry(review_id: int) -> dict:
        try:
            return retry_source_review(settings, review_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/maintenance")
    def get_maintenance(target_items: int = Query(500, ge=1, le=100000)) -> dict:
        return maintenance_status(settings, target_items)

    # Keep the historical handler implementations in this module for one
    # compatibility cycle, but remove their decorators from the application
    # router.  The middleware above owns the retired-address JSON 404 contract;
    # keeping these routes registered would leave a second Web product surface.
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not getattr(route, "path", "").startswith(RETIRED_API_PREFIXES)
    ]

    static_dir = Path(__file__).with_name("static")
    assets_dir = static_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        # Unmatched API routes must return JSON, never the SPA fallback HTML.
        # Otherwise the console shows the opaque "非 JSON 内容" error and an
        # operator cannot tell a stale/old backend (missing route) from a real
        # fault. A JSON 404 points straight at "restart/rebuild the backend".
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API route not found")
        index = static_dir / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=503, detail="Web frontend has not been built")
        return FileResponse(index)

    # Registered last so it is the outermost middleware: it populates
    # ``request.session`` (used by the auth gate in ``local_security``) before any
    # downstream middleware or route handler runs.
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="oa_local_session",
        same_site="strict",
        https_only=False,
        max_age=8 * 60 * 60,
    )

    return app


def _allowed_origins(settings: Settings) -> set[str]:
    return {
        f"http://127.0.0.1:{settings.web.port}",
        f"http://localhost:{settings.web.port}",
        f"http://[::1]:{settings.web.port}",
    }


def _load_or_create_session_secret(runtime: Path) -> str:
    runtime.mkdir(parents=True, exist_ok=True)
    os.chmod(runtime, 0o700)
    path = runtime / "web-session.key"
    if not path.exists():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(secrets.token_urlsafe(48))
    os.chmod(path, 0o600)
    return path.read_text(encoding="ascii").strip()


def _load_or_create_bootstrap_token(runtime: Path) -> str:
    """One-time provisioning credential printed to the launching user's terminal.

    Any local process that can read this file (owner-only, mode 0600) may exchange
    it for a session via ``POST /api/auth/login``. The web console is bound to a
    loopback address (see ``WebConfig.loopback_only``) and rejects non-loopback
    ``Host`` headers, so the token only matters for processes already on the same
    host. Treat it like a Jupyter notebook token: shown once at startup, paste it
    into the console login screen.
    """
    runtime.mkdir(parents=True, exist_ok=True)
    os.chmod(runtime, 0o700)
    path = runtime / "web-bootstrap.token"
    if not path.exists():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(secrets.token_urlsafe(32))
    os.chmod(path, 0o600)
    return path.read_text(encoding="ascii").strip()
