from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from oa_knowledge.config import Settings, load_settings
from oa_knowledge.web.provider_settings import provider_settings_view, update_provider_settings
from oa_knowledge.web.status import (
    batch_items_preview,
    cancel_archive_batch,
    create_discovery_job,
    create_policies_bulk,
    create_policy,
    dashboard_status,
    delete_policy,
    freeze_archive_batch,
    full_manifest_report_path,
    full_manifest_status,
    start_full_manifest_job,
    start_done_incremental_job,
    item_detail,
    job_progress,
    list_batches,
    list_discovery_jobs,
    list_events,
    list_items,
    list_manifest_items,
    mark_manifest_manual_review,
    manifest_item_detail,
    open_archived_file,
    latest_backfill_campaign,
    list_policies,
    list_reviews,
    maintenance_status,
    pause_archive_batch,
    preview_policy_hits,
    resume_archive_batch,
    retry_batch_items,
    retry_manifest_failed_items,
    recheck_manifest_no_attachment,
    audit_all_manifest_items,
    resolve_review,
    start_archive_job,
    start_backfill_campaign,
)
from oa_knowledge.web.lifecycle_views import (
    done_list as lifecycle_done_list,
    knowledge_detail as lifecycle_knowledge_detail,
    knowledge_list as lifecycle_knowledge_list,
    pending_detail as lifecycle_pending_detail,
    pending_list as lifecycle_pending_list,
    processing_center as lifecycle_processing_center,
    system_view as lifecycle_system_view,
)


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


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
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
    if not settings.database_path.exists():
        raise RuntimeError("database not initialized; run 'oa init' first")
    secret = _load_or_create_session_secret(settings.data_root)
    app = FastAPI(title="OA Knowledge Hub", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="oa_local_session",
        same_site="strict",
        https_only=False,
        max_age=8 * 60 * 60,
    )

    @app.middleware("http")
    async def local_security(request: Request, call_next):
        host = request.url.hostname
        if host not in {"127.0.0.1", "localhost", "::1", "testserver"}:
            return JSONResponse({"detail": "loopback host required"}, status_code=400)
        origin = request.headers.get("origin")
        if origin and origin not in _allowed_origins(settings):
            return JSONResponse({"detail": "cross-origin request rejected"}, status_code=403)
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
    def get_reviews(status: str = Query("pending")) -> list[dict]:
        try:
            return list_reviews(settings, status)
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

    @app.get("/api/maintenance")
    def get_maintenance(target_items: int = Query(500, ge=1, le=100000)) -> dict:
        return maintenance_status(settings, target_items)

    static_dir = Path(__file__).with_name("static")
    assets_dir = static_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        index = static_dir / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=503, detail="Web frontend has not been built")
        return FileResponse(index)

    return app


def _allowed_origins(settings: Settings) -> set[str]:
    return {
        f"http://127.0.0.1:{settings.web.port}",
        f"http://localhost:{settings.web.port}",
        f"http://[::1]:{settings.web.port}",
    }


def _load_or_create_session_secret(data_root: Path) -> str:
    runtime = data_root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    os.chmod(runtime, 0o700)
    path = runtime / "web-session.key"
    if not path.exists():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(secrets.token_urlsafe(48))
    os.chmod(path, 0o600)
    return path.read_text(encoding="ascii").strip()
