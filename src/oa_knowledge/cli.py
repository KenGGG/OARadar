from __future__ import annotations

import json
import re
import os
import sqlite3
import fcntl
import signal
from threading import Event, Thread
from dataclasses import asdict
from time import monotonic
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from uuid import uuid4

import typer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings, load_settings, validate_feishu_runtime_config
from oa_knowledge.batches import BatchPlan, apply_business_exclusions, apply_discovery, batch_dict, cancel_batch, freeze_batch, get_batch, pause_batch, plan_batch, recover_interrupted_items, resume_batch, retry_batch_item, retry_failed_items, reuse_verified_items, validate_batch
from oa_knowledge.backfill import BackfillWindow, next_month_remainder, shrink_latest
from oa_knowledge.collector import BrowserSession, CollaborationDetailAdapter, DoneAdapter, LoginState
from oa_knowledge.collector.detail import AuthRequiredError
from oa_knowledge.constants import BatchStatus, LEASE_TTL
from oa_knowledge.archive_reconciliation import migrate_archive_paths
from oa_knowledge.detail_archive import archive_collaboration_detail
from oa_knowledge.archive.integrity import sha256_file
from oa_knowledge.archive.naming import validate_relative_path
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, BatchItem, CollectionBatch, ExclusionPolicy, ItemOccurrence, OAItem, OAManifestItem, OAManifestSync, OperationEvent, OperationJob, ParseJob, PipelineTask, ReviewEntry, Run
from oa_knowledge.collector.done import DoneDiscovery
from oa_knowledge.collector.pending import PENDING_LIST_PATH, PendingAdapter
from oa_knowledge.collector.pending_detail import extract_pending_detail_identifiers
from oa_knowledge.full_manifest import archive_proxy, classify_manifest, classify_manifest_rows, effective_exclusion_keywords, export_manifest_csv, finalize_manifest_sync, manifest_counts, reuse_existing_archive, synchronize_manifest, upsert_manifest_page
from oa_knowledge.ops.audit import audit_database
from oa_knowledge.ops.capacity import capacity_report, scale_capacity_report
from oa_knowledge.ops.doctor import run_doctor
from oa_knowledge.ops.exclusion_cleanup import cleanup_excluded_archives
from oa_knowledge.pending_sync import apply_pending_identifiers, sync_pending_discovery
from oa_knowledge.pending_archive import persist_pending_capture
from oa_knowledge.resources import ResourceCoordinator
from oa_knowledge.scheduled_sync import (
    run_bootstrap_scan,
    run_hourly_scan,
    run_nightly_scan,
)
from oa_knowledge.reconcile import reconcile_done_occurrence
from oa_knowledge.source_roles import MARKDOWN_SOURCE_ROLES
from oa_knowledge.markdown_export.service import convert_archive, markdown_status as get_markdown_status

app = typer.Typer(help="OARadar V2 local read-only OA workspace")
db_app = typer.Typer(help="Database migration commands")
batch_app = typer.Typer(help="Immutable historical collection batch planning")
backfill_app = typer.Typer(help="Stage 2A-7 gated historical backfill")
parse_app = typer.Typer(help="Stage 3 document parsing pipeline")
manifest_app = typer.Typer(help="Canonical full Done-list synchronization and selective download")
pending_app = typer.Typer(help="Read-only Pending-list discovery")
archive_app = typer.Typer(help="Local archive path reconciliation and migration")
app.add_typer(db_app, name="db")
app.add_typer(batch_app, name="batch")
app.add_typer(backfill_app, name="backfill", hidden=True)
app.add_typer(parse_app, name="parse")
app.add_typer(manifest_app, name="manifest")
app.add_typer(pending_app, name="pending")
app.add_typer(archive_app, name="archive")
schedule_app = typer.Typer(help="Durable scheduled Pending/Done sync orchestration")
app.add_typer(schedule_app, name="schedule")
notifications_app = typer.Typer(help="Feishu delivery lifecycle and operational controls")
app.add_typer(notifications_app, name="notifications")
knowledge_app = typer.Typer(help="Done-archive to Markdown knowledge handoff")
app.add_typer(knowledge_app, name="knowledge", hidden=True)
curate_app = typer.Typer(help="Local-only OA Package to curated knowledge documents")
app.add_typer(curate_app, name="curate", hidden=True)
data_app = typer.Typer(help="本地数据预检、隔离、恢复与清除")
app.add_typer(data_app, name="data", hidden=True)


def settings_option(config: Path | None) -> Settings:
    return load_settings(config)


def secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def require_engine(settings: Settings):
    if not settings.database_path.exists():
        typer.echo("database not initialized; run 'oa init' first", err=True)
        raise typer.Exit(1)
    return create_db_engine(settings.database_path)


def _has_verified_attachment(session: Session, oa_item_key: str) -> bool:
    return bool(session.scalar(
        select(func.count()).select_from(ArchivedFile).join(OAItem).where(
            OAItem.oa_item_key == oa_item_key,
            ArchivedFile.file_role.in_(MARKDOWN_SOURCE_ROLES),
            ArchivedFile.download_status == "verified",
            ArchivedFile.local_relpath.is_not(None),
        )
    ))


@pending_app.command("discover")
def pending_discover(
    limit: int = typer.Option(3, "--limit", min=1, max=500),
    max_pages: int = typer.Option(1, "--max-pages", min=1, max=50),
    headed: bool = typer.Option(False, "--headed"),
    notify_mode: str = typer.Option("normal", "--notify-mode", help="normal | baseline | disabled"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Read Pending-list titles and metadata; never opens item details."""
    settings = settings_option(config)
    engine = require_engine(settings)
    coordinator = ResourceCoordinator(engine)
    owner = f"pending-discovery:{uuid4().hex}"
    lease_id = coordinator.acquire("oa_browser", owner, ttl_seconds=180, uses_local_gpu=False)
    if lease_id is None:
        typer.echo("OA browser is busy", err=True)
        raise typer.Exit(2)
    try:
        with BrowserSession(settings, headed=headed) as browser:
            if browser.login_with_saved_credentials(30) != LoginState.AUTHENTICATED:
                typer.echo("OA authentication required; rerun with --headed or run 'oa login'", err=True)
                raise typer.Exit(3)
            assert browser.page
            adapter = PendingAdapter(browser.page, f"{browser.base_url}{PENDING_LIST_PATH}")
            discovery = adapter.discover_pages(
                limit=limit,
                max_pages=max_pages,
                page_delay_seconds=settings.collector.list_page_delay_seconds,
            )
        with Session(engine) as session:
            complete_snapshot = (
                discovery.source_total_count is not None
                and len(discovery.items) >= discovery.source_total_count
                and discovery.scanned_row_count >= discovery.source_total_count
            ) or (
                discovery.source_total_pages is not None
                and discovery.pages_scanned >= discovery.source_total_pages
                and (
                    discovery.source_total_count is None
                    or len(discovery.items) >= discovery.source_total_count
                )
            )
            result = sync_pending_discovery(
                session,
                list(discovery.items),
                authoritative=complete_snapshot,
                notification_mode=notify_mode,
            )
            session.commit()
        typer.echo(json.dumps({
            "created": result.created,
            "updated": result.updated,
            "unchanged": result.unchanged,
            "closed": result.closed,
            "reactivated": result.reactivated,
            "pages_scanned": discovery.pages_scanned,
            "query_count": discovery.query_count,
            "source_total_count": discovery.source_total_count,
        }, ensure_ascii=False))
    finally:
        coordinator.release(lease_id, owner)


@pending_app.command("inspect-identities")
def pending_inspect_identities(
    limit: int = typer.Option(3, "--limit", min=1, max=500),
    headed: bool = typer.Option(False, "--headed"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Read identity metadata for discovered Pending rows; no body or attachments."""
    settings = settings_option(config)
    engine = require_engine(settings)
    with Session(engine) as session:
        targets = session.scalars(
            select(ItemOccurrence)
            .where(ItemOccurrence.channel == "pending")
            .order_by(ItemOccurrence.id)
            .limit(limit)
        ).all()
        target_rows = [(row.occurrence_key, row.affair_id_text) for row in targets if row.affair_id_text]

    coordinator = ResourceCoordinator(engine)
    owner = f"pending-identities:{uuid4().hex}"
    lease_id = coordinator.acquire("oa_browser", owner, ttl_seconds=300, uses_local_gpu=False)
    if lease_id is None:
        typer.echo("OA browser is busy", err=True)
        raise typer.Exit(2)
    inspected: list[tuple[str, object]] = []
    failed = 0
    try:
        with BrowserSession(settings, headed=headed) as browser:
            if browser.login_with_saved_credentials(30) != LoginState.AUTHENTICATED:
                typer.echo("OA authentication required; rerun with --headed or run 'oa login'", err=True)
                raise typer.Exit(3)
            assert browser.page
            for occurrence_key, affair_id in target_rows:
                try:
                    browser.page.goto(
                        PendingAdapter.detail_url(browser.base_url, affair_id),
                        wait_until="domcontentloaded",
                    )
                    browser.page.wait_for_timeout(1500)
                    inspected.append((occurrence_key, extract_pending_detail_identifiers(browser.page)))
                except Exception:
                    failed += 1
        with Session(engine) as session:
            for occurrence_key, identifiers in inspected:
                apply_pending_identifiers(session, occurrence_key, identifiers)
            session.commit()
        typer.echo(json.dumps({"inspected": len(inspected), "failed": failed}, ensure_ascii=False))
    finally:
        coordinator.release(lease_id, owner)


@pending_app.command("capture-all")
def pending_capture_all(
    limit: int = typer.Option(500, "--limit", min=1, max=500),
    headed: bool = typer.Option(False, "--headed"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Capture every current Pending item, including recipient-linked attachments."""
    settings = settings_option(config)
    engine = require_engine(settings)
    with Session(engine) as session:
        targets = session.scalars(
            select(ItemOccurrence).where(ItemOccurrence.channel == "pending")
            .order_by(ItemOccurrence.received_at.desc(), ItemOccurrence.id).limit(limit)
        ).all()
        target_rows = [(row.occurrence_key, row.affair_id_text, row.title) for row in targets if row.affair_id_text]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        job = OperationJob(
            job_key=f"pending-capture-{stamp}", job_type="pending_capture", status="running",
            idempotency_key=f"pending-capture-{stamp}",
            parameters_json=json.dumps({"limit": limit}, ensure_ascii=False),
            progress_total=len(target_rows), progress_current=0,
            lease_owner=f"pending-capture-cli:{stamp}",
            started_at=datetime.now(timezone.utc), heartbeat_at=datetime.now(timezone.utc),
        )
        session.add(job); session.commit(); job_id = job.id

    coordinator = ResourceCoordinator(engine)
    owner = f"pending-capture:{uuid4().hex}"
    lease_id = coordinator.acquire(
        "oa_browser", owner,
        ttl_seconds=max(600, len(target_rows) * settings.collector.attachment_total_timeout_seconds),
        uses_local_gpu=False,
    )
    if lease_id is None:
        with Session(engine) as session:
            job = session.get(OperationJob, job_id); assert job is not None
            job.status = "failed"; job.last_error_code = "oa_browser_busy"; job.finished_at = datetime.now(timezone.utc)
            session.commit()
        typer.echo("OA browser is busy", err=True)
        raise typer.Exit(2)

    failed = verified = 0
    try:
        with BrowserSession(settings, headed=headed) as browser:
            if browser.login_with_saved_credentials(30) != LoginState.AUTHENTICATED:
                with Session(engine) as session:
                    job = session.get(OperationJob, job_id); assert job is not None
                    job.status = "auth_required"; job.last_error_code = "auth_required"; job.finished_at = datetime.now(timezone.utc)
                    session.commit()
                typer.echo("OA authentication required; rerun with --headed or run 'oa login'", err=True)
                raise typer.Exit(3)
            assert browser.page
            adapter = CollaborationDetailAdapter(
                browser.page, attachment_resolver=verified_attachment_resolver(engine, settings.data_root),
            )
            for sequence, (occurrence_key, affair_id, title) in enumerate(target_rows, 1):
                with Session(engine) as session:
                    job = session.get(OperationJob, job_id); assert job is not None
                    session.add(OperationEvent(
                        job_id=job_id, sequence=sequence * 2 - 1, event_type="item", status="running",
                        details_json=json.dumps({"title": title, "attachment_verified": 0, "attachment_total": 0}, ensure_ascii=False),
                    ))
                    job.heartbeat_at = datetime.now(timezone.utc); session.commit()
                try:
                    capture = adapter.capture(
                        affair_id,
                        max_depth=settings.collector.max_attachment_depth,
                        total_timeout_seconds=settings.collector.attachment_total_timeout_seconds,
                        download_timeout_seconds=settings.collector.download_timeout_seconds,
                        direct_url=PendingAdapter.detail_url(browser.base_url, affair_id),
                    )
                    with Session(engine) as session:
                        result = persist_pending_capture(session, occurrence_key, capture, settings.data_root)
                        job = session.get(OperationJob, job_id); assert job is not None
                        job.progress_current = sequence; job.heartbeat_at = datetime.now(timezone.utc)
                        session.add(OperationEvent(
                            job_id=job_id, sequence=sequence * 2, event_type="item",
                            status="completed" if result.failed_attachment_count == 0 else "failed",
                            details_json=json.dumps({
                                "title": title, "attachment_verified": result.verified_attachment_count,
                                "attachment_total": result.source_attachment_count,
                                "attachment_failed": result.failed_attachment_count,
                            }, ensure_ascii=False),
                        ))
                        session.commit()
                    verified += result.verified_attachment_count
                    failed += int(result.failed_attachment_count > 0)
                except Exception as exc:
                    failed += 1
                    with Session(engine) as session:
                        job = session.get(OperationJob, job_id); assert job is not None
                        job.progress_current = sequence; job.heartbeat_at = datetime.now(timezone.utc)
                        session.add(OperationEvent(
                            job_id=job_id, sequence=sequence * 2, event_type="item", status="failed",
                            details_json=json.dumps({"title": title, "error_code": type(exc).__name__}, ensure_ascii=False),
                        ))
                        session.add(ReviewEntry(
                            kind="pending_capture_failed", container_key=occurrence_key,
                            details_json=json.dumps({"error": _sanitize_operational_error(exc)}, ensure_ascii=False),
                        ))
                        session.commit()
                browser.page.wait_for_timeout(int(settings.collector.item_delay_seconds * 1000))
        with Session(engine) as session:
            job = session.get(OperationJob, job_id); assert job is not None
            job.status = "completed" if failed == 0 else "failed"
            job.last_error_code = None if failed == 0 else "item_failures"
            job.finished_at = datetime.now(timezone.utc); session.commit()
        typer.echo(json.dumps({"job_id": job_id, "processed": len(target_rows), "failed": failed, "attachments_verified": verified}, ensure_ascii=False))
    except typer.Exit:
        raise
    except Exception as exc:
        with Session(engine) as session:
            job = session.get(OperationJob, job_id)
            if job is not None and job.status in {"queued", "running"}:
                job.status = "failed"
                job.last_error_code = type(exc).__name__
                job.finished_at = datetime.now(timezone.utc)
                session.commit()
        raise
    finally:
        coordinator.release(lease_id, owner)


@manifest_app.command("sync")
def manifest_sync(
    headed: bool = typer.Option(False, "--headed"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Synchronize every Done-list row; never opens a detail page."""
    settings = settings_option(config)
    engine = require_engine(settings)
    with BrowserSession(settings, headed=headed) as browser:
        if browser.login_with_saved_credentials(30) != LoginState.AUTHENTICATED:
            typer.echo("OA authentication required", err=True)
            raise typer.Exit(3)
        assert browser.page
        discovery = DoneAdapter(browser.page, f"{browser.base_url}{settings.browser.done_list_path}").discover_all_pages(
            page_delay_seconds=settings.collector.list_page_delay_seconds,
        )
    with Session(engine) as session:
        sync = synchronize_manifest(session, discovery)
        report_path = export_manifest_csv(session, settings.data_root)
        session.commit()
        payload = {
            "status": sync.status, "oa_total_count": sync.oa_total_count,
            "local_manifest_count": sync.local_manifest_count,
            "pages_scanned": sync.pages_scanned, "source_total_pages": sync.source_total_pages,
            "report": report_path.relative_to(settings.data_root).as_posix(),
        }
    typer.echo(json.dumps(payload, ensure_ascii=False))


@manifest_app.command("refresh-head")
def manifest_refresh_head(
    max_pages: int = typer.Option(3, "--max-pages", min=1, max=20),
    headed: bool = typer.Option(False, "--headed"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Refresh OA totals and the newest Done-list page without scanning all pages."""
    settings = settings_option(config); engine = require_engine(settings)
    started_at = datetime.now(timezone.utc)
    coordinator = ResourceCoordinator(engine)
    owner = f"done-incremental:{uuid4().hex}"
    lease_id = coordinator.acquire("oa_browser", owner, ttl_seconds=900, uses_local_gpu=False)
    if lease_id is None:
        typer.echo("OA browser is busy", err=True); raise typer.Exit(2)
    try:
        with BrowserSession(settings, headed=headed) as browser:
            if browser.login_with_saved_credentials(30) != LoginState.AUTHENTICATED:
                typer.echo("OA authentication required", err=True); raise typer.Exit(3)
            assert browser.page
            adapter = DoneAdapter(browser.page, f"{browser.base_url}{settings.browser.done_list_path}")
            discovery = adapter.discover_pages(
                limit=10_000,
                max_pages=max_pages,
                page_delay_seconds=settings.collector.list_page_delay_seconds,
            )
            source_total, source_pages = discovery.source_total_count, discovery.source_total_pages
            if source_total is None or source_pages is None:
                typer.echo("OA done-list totals unavailable", err=True); raise typer.Exit(4)
            newest = list(discovery.items)
    finally:
        coordinator.release(lease_id, owner)
    with Session(engine) as session:
        before = session.scalar(select(func.count()).select_from(OAManifestItem)) or 0
        upsert_manifest_page(session, newest, started_at)
        rows = session.scalars(select(OAManifestItem).where(OAManifestItem.oa_item_key.in_([item.oa_item_key for item in newest]))).all()
        classify_manifest_rows(session, rows, effective_exclusion_keywords(session), settings.data_root)
        target_keys = [row.oa_item_key for row in rows if row.processing_status in {"pending_download", "download_failed"}]
        active_download = session.scalar(select(OperationJob).where(
            OperationJob.job_type == "full_manifest_retry",
            OperationJob.status.in_(("queued", "running")),
        ).limit(1))
        if target_keys and active_download is None:
            download_job = OperationJob(
                job_key=f"done-incremental-download-{uuid4().hex[:12]}",
                job_type="full_manifest_retry",
                status="queued",
                idempotency_key=f"done-incremental-download-{uuid4().hex}",
                parameters_json=json.dumps({"oa_item_keys": target_keys, "source_status": "pending_download"}, ensure_ascii=False),
                progress_total=len(target_keys),
            )
            session.add(download_job)
        local_count = session.scalar(select(func.count()).select_from(OAManifestItem)) or 0
        refresh_status = "manifest_complete" if local_count == source_total else "manifest_incomplete"
        sync = OAManifestSync(
            oa_total_count=source_total, local_manifest_count=local_count,
            pages_scanned=discovery.pages_scanned, source_total_pages=source_pages,
            status=refresh_status,
            started_at=started_at, finished_at=datetime.now(timezone.utc),
        )
        session.add(sync); session.commit()
    typer.echo(json.dumps({"oa_total_count": source_total, "local_manifest_count": local_count, "source_total_pages": source_pages, "pages_scanned": discovery.pages_scanned, "new_items": max(0, local_count - before)}, ensure_ascii=False))


@manifest_app.command("run")
def manifest_run(
    max_pages: int | None = typer.Option(None, "--max-pages", min=1),
    max_items: int | None = typer.Option(None, "--max-items", min=1),
    headed: bool = typer.Option(False, "--headed"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Page pipeline: persist list page, classify/reuse/download it, then advance.

    ``--max-pages`` / ``--max-items`` bound this existing flow for a small
    verification run; bounded runs deliberately skip full-manifest reconciliation.
    """
    settings = settings_option(config); engine = require_engine(settings)
    started_at = datetime.now(timezone.utc)
    all_items: dict[str, object] = {}
    scanned_rows = 0
    pages_scanned = 0
    auth_retry_counts: dict[int, int] = {}
    with BrowserSession(settings, headed=headed) as browser:
        if browser.login_with_saved_credentials(30) != LoginState.AUTHENTICATED:
            typer.echo("OA authentication required", err=True); raise typer.Exit(3)
        assert browser.page
        list_adapter = DoneAdapter(browser.page, f"{browser.base_url}{settings.browser.done_list_path}")
        frame = list_adapter.open_list()
        source_total, source_pages = list_adapter._list_stats(frame)
        if source_total is None or source_pages is None:
            typer.echo("OA total count/page count unavailable", err=True); raise typer.Exit(4)
        detail_adapter = CollaborationDetailAdapter(
            browser.page, attachment_resolver=verified_attachment_resolver(engine, settings.data_root),
        )
        page_ceiling = min(source_pages, max_pages) if max_pages else source_pages
        for page_number in range(1, page_ceiling + 1):
            remaining = max_items - len(all_items) if max_items else 10_000
            if remaining <= 0:
                break
            page_items = list_adapter._discover_frame(frame, remaining, page_number, scanned_rows)
            scanned_rows += len(page_items); pages_scanned += 1
            for item in page_items:
                all_items.setdefault(item.oa_item_key, item)
            keys = [item.oa_item_key for item in page_items]
            with Session(engine) as session:
                upsert_manifest_page(session, page_items)
                rows = session.scalars(select(OAManifestItem).where(OAManifestItem.oa_item_key.in_(keys))).all() if keys else []
                classify_manifest_rows(session, rows, effective_exclusion_keywords(session), settings.data_root)
                session.commit()
                row_ids = [row.id for row in rows if row.processing_status in {"pending_download", "download_failed"}]
            for row_id in row_ids:
                with Session(engine) as session:
                    row = session.get(OAManifestItem, row_id); assert row is not None
                    # Re-check because a prior item may have supplied a reusable attachment.
                    classify_manifest_rows(session, [row], effective_exclusion_keywords(session), settings.data_root)
                    if row.processing_status not in {"pending_download", "download_failed"}:
                        session.commit(); continue
                    row.processing_status = "processing"; row.last_retry_at = datetime.now(timezone.utc)
                    workitem_id = row.workitem_id_text; session.commit()
                try:
                    if not workitem_id:
                        raise RuntimeError("OA item identifier unavailable")
                    capture = detail_adapter.capture_direct(
                        browser.base_url, workitem_id, max_depth=10,
                        total_timeout_seconds=settings.collector.attachment_total_timeout_seconds,
                        download_timeout_seconds=settings.collector.download_timeout_seconds,
                    )
                    with Session(engine) as session:
                        row = session.get(OAManifestItem, row_id); assert row is not None
                        proxy = archive_proxy(row); archive_collaboration_detail(session, proxy, capture, settings.data_root)
                        row.archive_relpath = session.scalar(select(OAItem.archive_relpath).where(OAItem.oa_item_key == row.oa_item_key))
                        attachments = list(capture.attachments) + [a for container in capture.related_containers for a in container.attachments]
                        if proxy.archive_status == "archived":
                            row.processing_status = "downloaded" if attachments or _has_verified_attachment(session, row.oa_item_key) else "no_attachment"
                            row.last_error = None; row.failure_stage = None
                        else:
                            row.processing_status = "download_failed"; row.retry_count += 1
                            row.last_error = proxy.last_error or _attachment_failure_summary(capture); row.failure_stage = "attachment"
                        session.commit()
                except AuthRequiredError as exc:
                    # OA sessions can expire during a long full scan.  Restore
                    # the authenticated list page and append this row back to
                    # the current page's work queue instead of letting a stale
                    # frame abort the whole manifest process.
                    auth_retry_counts[row_id] = auth_retry_counts.get(row_id, 0) + 1
                    if (
                        auth_retry_counts[row_id] <= 2
                        and browser.login_with_saved_credentials(30) == LoginState.AUTHENTICATED
                    ):
                        frame = list_adapter.navigate_to_page(
                            page_number, settings.collector.list_page_delay_seconds,
                        )
                        with Session(engine) as session:
                            row = session.get(OAManifestItem, row_id)
                            if row is not None:
                                row.processing_status = "pending_download"
                                row.last_error = None
                                row.failure_stage = None
                                row.last_retry_at = datetime.now(timezone.utc)
                                session.commit()
                        row_ids.append(row_id)
                        continue
                    with Session(engine) as session:
                        row = session.get(OAManifestItem, row_id)
                        if row is not None:
                            row.processing_status = "auth_required"
                            row.last_error = _sanitize_operational_error(exc)
                            row.failure_stage = "authentication"
                            row.last_retry_at = datetime.now(timezone.utc)
                            session.commit()
                    typer.echo("OA authentication required", err=True)
                    raise typer.Exit(3)
                except Exception as exc:
                    with Session(engine) as session:
                        row = session.get(OAManifestItem, row_id)
                        if row is not None:
                            row.processing_status = "download_failed"; row.retry_count += 1
                            row.last_error = _sanitize_operational_error(exc); row.failure_stage = "detail_or_download"
                            row.last_retry_at = datetime.now(timezone.utc); session.commit()
                browser.page.wait_for_timeout(int(settings.collector.item_delay_seconds * 1000))
            if len(all_items) >= (max_items or source_total):
                break
            if page_number < page_ceiling and not list_adapter._next_page(frame, settings.collector.list_page_delay_seconds):
                break
    bounded = max_pages is not None or max_items is not None
    if bounded:
        with Session(engine) as session:
            counts = manifest_counts(session)
            session.commit()
        typer.echo(json.dumps(counts | {
            "mode": "bounded_existing_manifest_run", "pages_scanned": pages_scanned,
            "items_read": len(all_items), "source_total_pages": source_pages,
        }, ensure_ascii=False))
        return
    discovery = DoneDiscovery(
            tuple(all_items.values()), pages_scanned, len(all_items), scanned_rows, source_total, source_pages,
        )
    with Session(engine) as session:
        sync = finalize_manifest_sync(session, discovery, started_at)
        counts = manifest_counts(session); report_path = export_manifest_csv(session, settings.data_root); session.commit()
        payload = counts | {
            "oa_total_count": source_total, "manifest_status": sync.status,
            "aligned": sync.status == "manifest_complete", "pages_scanned": pages_scanned,
            "source_total_pages": source_pages, "report": report_path.relative_to(settings.data_root).as_posix(),
        }
    typer.echo(json.dumps(payload, ensure_ascii=False))
    if sync.status != "manifest_complete":
        raise typer.Exit(4)


@manifest_app.command("classify")
def manifest_classify(
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Apply configured title keywords and reuse locally verified archives."""
    settings = settings_option(config); engine = require_engine(settings)
    with Session(engine) as session:
        counts = classify_manifest(session, effective_exclusion_keywords(session), settings.data_root)
        report_path = export_manifest_csv(session, settings.data_root)
        session.commit()
    typer.echo(json.dumps(counts | {"report": report_path.relative_to(settings.data_root).as_posix()}, ensure_ascii=False))


@manifest_app.command("download")
def manifest_download(
    max_items: int = typer.Option(20, "--max-items", min=1, max=10000),
    headed: bool = typer.Option(False, "--headed"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
    error_type: str | None = typer.Option(None, "--error-type"),
    item_id: str | None = typer.Option(None, "--item-id"),
    item_ids: str | None = typer.Option(None, "--item-ids"),
    failed_only: bool = typer.Option(False, "--failed-only"),
    recheck_no_attachment: bool = typer.Option(False, "--recheck-no-attachment"),
    audit_all: bool = typer.Option(False, "--audit-all"),
) -> None:
    """Download pending/failed items or recheck items previously marked no_attachment."""
    settings = settings_option(config); engine = require_engine(settings)
    with Session(engine) as session:
        latest = session.scalar(select(OAManifestSync).order_by(OAManifestSync.id.desc()).limit(1))
        local_count = session.scalar(select(func.count()).select_from(OAManifestItem)) or 0
        if latest is None or (latest.status != "manifest_complete" and local_count != latest.oa_total_count):
            typer.echo("manifest_incomplete: download is blocked", err=True); raise typer.Exit(4)
        # Re-evaluate every existing row against the latest config + Web policy
        # list, without rescanning OA. Successful/skipped rows remain terminal.
        classify_manifest(session, effective_exclusion_keywords(session), settings.data_root)
        session.commit()
    processed = 0
    attempted_ids: set[int] = set()
    with BrowserSession(settings, headed=headed) as browser:
        if browser.login_with_saved_credentials(30) != LoginState.AUTHENTICATED:
            typer.echo("OA authentication required", err=True); raise typer.Exit(3)
        assert browser.page
        list_adapter = DoneAdapter(browser.page, f"{browser.base_url}{settings.browser.done_list_path}")
        list_adapter.open_list()
        detail_adapter = CollaborationDetailAdapter(
            browser.page, attachment_resolver=verified_attachment_resolver(engine, settings.data_root),
        )
        current_page = 1
        while processed < max_items:
            with Session(engine) as session:
                if audit_all:
                    statuses = None
                elif recheck_no_attachment:
                    statuses = ("no_attachment",)
                else:
                    statuses = ("download_failed",) if failed_only else ("pending_download", "download_failed")
                query = select(OAManifestItem)
                if statuses is not None:
                    query = query.where(OAManifestItem.processing_status.in_(statuses))
                selected_item_ids = [value.strip() for value in (item_ids or "").split(",") if value.strip()]
                if item_ids:
                    query = query.where(OAManifestItem.oa_item_key.in_(selected_item_ids))
                elif item_id:
                    query = query.where((OAManifestItem.workitem_id_text == item_id) | (OAManifestItem.oa_item_key == item_id))
                if error_type == "meeting":
                    query = query.where(OAManifestItem.last_error.like("%meeting.do%"))
                elif error_type == "attachment":
                    query = query.where(OAManifestItem.last_error.like("%attachment verification failed%"))
                if attempted_ids:
                    query = query.where(OAManifestItem.id.not_in(attempted_ids))
                row = session.scalar(query.order_by(
                    OAManifestItem.retry_count.asc(), OAManifestItem.processing_status.desc(), OAManifestItem.completed_at.desc(), OAManifestItem.id.desc(),
                ).limit(1))
                if row is None:
                    break
                if audit_all and not _audit_opens_detail(row.processing_status):
                    row.last_retry_at = datetime.now(timezone.utc)
                    row.last_error = None
                    row.failure_stage = None
                    session.commit()
                    attempted_ids.add(row.id)
                    processed += 1
                    continue
                previous_status = row.processing_status
                row.processing_status = "processing"
                row.last_retry_at = datetime.now(timezone.utc)
                row_id, target_page, workitem_id, row_title, row_retry_count = row.id, row.list_page, row.workitem_id_text, row.title, row.retry_count
                session.commit()
                attempted_ids.add(row_id)
            try:
                if not workitem_id:
                    raise RuntimeError("OA item identifier unavailable")
                # Repeated failures are often large multi-archive items. Keep the
                # configured total budget instead of shortening later retries.
                item_total_timeout = settings.collector.attachment_total_timeout_seconds
                try:
                    capture = detail_adapter.capture_direct(
                        browser.base_url, workitem_id, max_depth=10,
                        total_timeout_seconds=item_total_timeout,
                        download_timeout_seconds=settings.collector.download_timeout_seconds,
                    )
                except AuthRequiredError:
                    raise
                except Exception as direct_exc:
                    try:
                        located_workitem_id = list_adapter.locate_item(
                            target_page, row_title, workitem_id,
                            settings.collector.list_page_delay_seconds,
                        )
                        current_page = target_page
                        capture = detail_adapter.capture(
                            located_workitem_id, max_depth=10,
                            total_timeout_seconds=item_total_timeout,
                            download_timeout_seconds=settings.collector.download_timeout_seconds,
                        )
                    except Exception as fallback_exc:
                        direct_error = _sanitize_operational_error(direct_exc)
                        fallback_error = _sanitize_operational_error(fallback_exc)
                        raise RuntimeError(f"direct detail failed: {direct_error}; list fallback failed: {fallback_error}") from fallback_exc
                try:
                    done_identifiers = extract_pending_detail_identifiers(browser.page)
                except Exception:
                    done_identifiers = None
                with Session(engine) as session:
                    row = session.get(OAManifestItem, row_id); assert row is not None
                    proxy = archive_proxy(row)
                    archive_collaboration_detail(session, proxy, capture, settings.data_root)
                    row.archive_relpath = session.scalar(select(OAItem.archive_relpath).where(OAItem.oa_item_key == row.oa_item_key))
                    attachments = list(capture.attachments) + [a for container in capture.related_containers for a in container.attachments]
                    if proxy.archive_status == "archived":
                        row.processing_status = "downloaded" if attachments or _has_verified_attachment(session, row.oa_item_key) else "no_attachment"
                        row.last_error = None; row.failure_stage = None
                    else:
                        row.processing_status = "download_failed"
                        row.retry_count += 1
                        row.last_error = proxy.last_error or _attachment_failure_summary(capture)
                        row.failure_stage = "attachment"
                    if done_identifiers and done_identifiers.affair_id_text:
                        candidate = session.scalar(select(ItemOccurrence.id).where(
                            ItemOccurrence.channel == "pending",
                            ItemOccurrence.affair_id_text == done_identifiers.affair_id_text,
                        ).limit(1))
                        if candidate is not None:
                            decision = reconcile_done_occurrence(
                                session,
                                identifiers=done_identifiers,
                                title=row.title,
                                sender=row.sender,
                                completed_at=row.completed_at,
                            )
                            if decision.outcome == "exact" and decision.logical_item_id is not None:
                                task_key = f"realtime-done:{row.oa_item_key}:final-v1"
                                if session.scalar(select(PipelineTask.id).where(PipelineTask.idempotency_key == task_key)) is None:
                                    session.add(PipelineTask(
                                        queue_name="realtime_done", priority=10,
                                        logical_item_key=row.oa_item_key,
                                        logical_item_id=decision.logical_item_id,
                                        stage="attachment_inventory",
                                        idempotency_key=task_key,
                                        payload_json=json.dumps({"manifest_id": row.id}),
                                    ))
                    session.commit()
            except AuthRequiredError as exc:
                if browser.login_with_saved_credentials(30) == LoginState.AUTHENTICATED:
                    with Session(engine) as session:
                        row = session.get(OAManifestItem, row_id)
                        if row is not None:
                            row.processing_status = previous_status
                            row.last_error = None; row.failure_stage = None
                            row.last_retry_at = datetime.now(timezone.utc); session.commit()
                    attempted_ids.discard(row_id)
                    continue
                with Session(engine) as session:
                    row = session.get(OAManifestItem, row_id)
                    if row is not None:
                        row.processing_status = "auth_required"; row.last_error = str(exc); row.failure_stage = "authentication"
                        row.last_retry_at = datetime.now(timezone.utc); session.commit()
                break
            except Exception as exc:
                with Session(engine) as session:
                    row = session.get(OAManifestItem, row_id)
                    if row is not None:
                        if reuse_existing_archive(session, row, settings.data_root):
                            row.last_error = None; row.failure_stage = None
                        else:
                            row.processing_status = "download_failed"; row.retry_count += 1
                            row.last_error = _sanitize_operational_error(exc); row.failure_stage = "detail_or_download"
                        row.last_retry_at = datetime.now(timezone.utc); session.commit()
            processed += 1
    with Session(engine) as session:
        counts = manifest_counts(session); report_path = export_manifest_csv(session, settings.data_root); session.commit()
    typer.echo(json.dumps(counts | {"processed": processed, "report": report_path.relative_to(settings.data_root).as_posix()}, ensure_ascii=False))


@app.command("retry-failed")
def retry_failed(
    error_type: str | None = typer.Option(None, "--error-type"),
    item_id: str | None = typer.Option(None, "--item-id"),
    max_items: int = typer.Option(100, "--max-items", min=1, max=100),
    headed: bool = typer.Option(False, "--headed"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Retry only rows currently marked download_failed."""
    manifest_download(max_items=max_items, headed=headed, config=config, error_type=error_type, item_id=item_id, failed_only=True)


@manifest_app.command("report")
def manifest_report(config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False)) -> None:
    settings = settings_option(config); engine = require_engine(settings)
    with Session(engine) as session:
        latest = session.scalar(select(OAManifestSync).order_by(OAManifestSync.id.desc()).limit(1))
        counts = manifest_counts(session); path = export_manifest_csv(session, settings.data_root); session.commit()
        payload = counts | {
            "oa_total_count": latest.oa_total_count if latest else None,
            "manifest_status": latest.status if latest else "manifest_incomplete",
            "aligned": bool(latest and latest.status == "manifest_complete"),
            "report": path.relative_to(settings.data_root).as_posix(),
        }
    typer.echo(json.dumps(payload, ensure_ascii=False))


def verified_attachment_resolver(engine, data_root: Path):
    """Return a process-local resolver that safely reuses verified OA attachment bytes."""
    cache: dict[str, tuple[bytes | None, str | None] | None] = {}

    def resolve(attachment_key: str) -> tuple[bytes | None, str | None] | None:
        if attachment_key in cache:
            return cache[attachment_key]
        with Session(engine) as session:
            candidates = session.scalars(
                select(ArchivedFile).where(
                    ArchivedFile.attachment_key == attachment_key,
                    ArchivedFile.download_status == "verified",
                    ArchivedFile.local_relpath.is_not(None),
                    ArchivedFile.sha256.is_not(None),
                    ArchivedFile.file_role.in_(MARKDOWN_SOURCE_ROLES),
                ).order_by(ArchivedFile.id.desc()).limit(3)
            ).all()
        for candidate in candidates:
            try:
                relative = validate_relative_path(candidate.local_relpath or "")
                path = data_root.joinpath(*relative.parts)
                if not path.is_file() or path.stat().st_size != candidate.size_bytes:
                    continue
                if sha256_file(path) != candidate.sha256:
                    continue
                result = (path.read_bytes(), candidate.mime_type)
                cache[attachment_key] = result
                return result
            except (OSError, ValueError):
                continue
        cache[attachment_key] = None
        return None

    return resolve


@app.command("init")
def init_command(config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False)) -> None:
    settings = settings_option(config)
    secure_dir(settings.data_root)
    for relative in ("state", "archive/raw/oa/pending", "archive/raw/oa/done", "parse/artifacts", "parse/staging", "parse/failed", "runtime", "logs", "backups", "workspace/raw/sources/oa/pending", "workspace/raw/sources/oa/done", "workspace/wiki"):
        secure_dir(settings.data_root / relative)
    upgrade_database(settings.database_path)
    os.chmod(settings.database_path, 0o600)
    typer.echo(f"initialized: {settings.data_root}")


@app.command("convert")
def convert_command(
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
    item: str | None = typer.Option(None, "--item"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    settings = settings_option(config)
    require_engine(settings).dispose()
    typer.echo(json.dumps(convert_archive(settings, item=item, force=force), ensure_ascii=False))


@app.command("rebuild-markdown")
def rebuild_markdown_command(config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False)) -> None:
    settings = settings_option(config)
    require_engine(settings).dispose()
    typer.echo(json.dumps(convert_archive(settings, force=True, rebuild=True), ensure_ascii=False))


@app.command("markdown-status")
def markdown_status_command(config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False)) -> None:
    settings = settings_option(config)
    require_engine(settings).dispose()
    typer.echo(json.dumps(get_markdown_status(settings), ensure_ascii=False))


@db_app.command("upgrade")
def db_upgrade(config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False)) -> None:
    settings = settings_option(config)
    secure_dir(settings.database_path.parent)
    upgrade_database(settings.database_path)
    os.chmod(settings.database_path, 0o600)
    typer.echo("database upgraded")


@app.command("doctor")
def doctor(config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False)) -> None:
    checks = run_doctor(settings_option(config))
    for check in checks:
        typer.echo(f"{'OK' if check.ok else 'FAIL'} {check.name}: {check.detail}")
    if any(not c.ok and c.required for c in checks):
        raise typer.Exit(1)


@app.command("status")
def status(config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False)) -> None:
    settings = settings_option(config)
    engine = require_engine(settings)
    with Session(engine) as session:
        counts = {
            "items": session.scalar(select(func.count()).select_from(OAItem)),
            "files": session.scalar(select(func.count()).select_from(ArchivedFile)),
            "batches": session.scalar(select(func.count()).select_from(CollectionBatch)),
            "runs": session.scalar(select(func.count()).select_from(Run)),
            "reviews": session.scalar(select(func.count()).select_from(ReviewEntry).where(ReviewEntry.status == "pending")),
        }
    with sqlite3.connect(settings.database_path) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    typer.echo(json.dumps({"schema": version, **counts}, ensure_ascii=False))


@app.command("capacity")
def capacity(
    target_items: int = typer.Option(500, "--target-items", min=1, max=100000),
    safety_factor: float = typer.Option(1.5, "--safety-factor", min=1, max=5),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    settings = settings_option(config)
    engine = require_engine(settings)
    engine.dispose()
    report = capacity_report(settings.database_path, settings.data_root, target_items, safety_factor)
    typer.echo(json.dumps(report.as_dict(), ensure_ascii=False))
    if not report.allowed:
        raise typer.Exit(1)


@app.command("scale-capacity")
def scale_capacity(
    target_items: int = typer.Option(500, "--target-items", min=1, max=100000),
    safety_factor: float = typer.Option(1.5, "--safety-factor", min=1, max=5),
    min_free_percent: float = typer.Option(10.0, "--min-free-percent", min=0, max=100),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Scale-500 gate: check disk capacity with percentage threshold and warnings."""
    settings = settings_option(config)
    engine = require_engine(settings)
    engine.dispose()
    report = scale_capacity_report(
        settings.database_path, settings.data_root, target_items, safety_factor, min_free_percent,
    )
    typer.echo(json.dumps(report.as_dict(), ensure_ascii=False))
    if not report.allowed:
        for w in report.warnings:
            typer.echo(f"WARNING: {w}", err=True)
        raise typer.Exit(1)


@app.command("audit")
def audit(config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False)) -> None:
    issues = audit_database(settings_option(config))
    if not issues:
        typer.echo("audit ok")
        return
    for issue in issues:
        typer.echo(f"{issue.code} id={issue.record_id}: {issue.detail}")
    raise typer.Exit(1)


@app.command("cleanup-excluded")
def cleanup_excluded(
    execute: bool = typer.Option(False, "--execute", help="Permanently delete matched local archives"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    if not execute:
        typer.echo("refusing destructive cleanup without --execute", err=True)
        raise typer.Exit(2)
    result = cleanup_excluded_archives(settings_option(config))
    typer.echo(json.dumps(asdict(result), ensure_ascii=False))


@app.command("login")
def login(
    wait_seconds: int = typer.Option(300, "--wait-seconds", min=10, max=900),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    settings = settings_option(config)
    with BrowserSession(settings, headed=True) as browser:
        state = browser.login_with_saved_credentials(wait_seconds)
    typer.echo(str(state))
    if state != LoginState.AUTHENTICATED:
        raise typer.Exit(3)


@app.command("web")
def web(
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Run the loopback-only local management console."""
    settings = settings_option(config)
    require_engine(settings).dispose()
    from oa_knowledge.web import create_web_app
    import uvicorn

    typer.echo(f"OA Web console: http://{settings.web.host}:{settings.web.port}")
    from oa_knowledge.web.app import _load_or_create_bootstrap_token
    bootstrap_token = _load_or_create_bootstrap_token(settings.runtime_root)
    if settings.web.require_auth:
        typer.echo("Web API authentication is ENABLED.")
        typer.echo(f"Console bootstrap token: {bootstrap_token}")
        typer.echo("Paste this token into the console login screen (shown once).")
    uvicorn.run(create_web_app(settings, config_path=config), host=settings.web.host, port=settings.web.port, access_log=False)


@app.command("worker")
def worker(
    once: bool = typer.Option(False, "--once", help="Process at most one queued job"),
    poll_seconds: float = typer.Option(2.0, "--poll-seconds", min=0.2, max=60),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Run the single durable worker for Web-enqueued read-only OA jobs."""
    settings = settings_option(config)
    require_engine(settings).dispose()
    lock_path = settings.runtime_root / "operation-worker.lock"
    secure_dir(lock_path.parent)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            typer.echo("another operation worker is already running", err=True)
            raise typer.Exit(1) from exc
        os.chmod(lock_path, 0o600)
        from oa_knowledge.web.worker import OperationWorker

        operation_worker = OperationWorker(settings, config_path=config)
        try:
            recovered = operation_worker.recover_expired()
            typer.echo(f"worker started: owner={operation_worker.owner} recovered={recovered}")
            if once:
                operation_worker.run_once()
            else:
                operation_worker.run_forever(poll_seconds=poll_seconds)
        finally:
            operation_worker.close()


@app.command("markdown-worker")
def markdown_worker_command(
    once: bool = typer.Option(False, "--once"),
    poll_seconds: float = typer.Option(2.0, "--poll-seconds", min=0.2, max=60),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Run the local-only Markdown conversion worker."""
    settings = settings_option(config); require_engine(settings).dispose()
    lock_path=settings.runtime_root / "markdown-worker.lock"; secure_dir(lock_path.parent)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try: fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc: raise typer.Exit(1) from exc
        from oa_knowledge.markdown_worker import MarkdownWorker
        runner=MarkdownWorker(settings)
        try:
            while True:
                handled=runner.run_once()
                if once: break
                if not handled: Event().wait(poll_seconds)
        finally: runner.close()


@batch_app.command("plan")
def batch_plan(
    from_date: str = typer.Option(..., "--from", help="Inclusive local date (YYYY-MM-DD)"),
    to_date: str = typer.Option(..., "--to", help="Inclusive local date (YYYY-MM-DD)"),
    source: str = typer.Option("done", "--source"),
    window_field: str = typer.Option("completed_at", "--window-field"),
    max_items: int = typer.Option(20, "--max-items", min=1, max=500),
    notes: str | None = typer.Option(None, "--notes"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    settings = settings_option(config)
    try:
        start_date = date.fromisoformat(from_date)
        end_date = date.fromisoformat(to_date)
        zone = ZoneInfo(settings.app.timezone)
        start = datetime.combine(start_date, time.min, zone)
        end = datetime.combine(end_date + timedelta(days=1), time.min, zone)
        with Session(require_engine(settings)) as session:
            batch, created = plan_batch(session, BatchPlan(source, start, end, window_field, max_items, notes))
            session.commit()
            payload = batch_dict(batch)
            payload["created"] = created
            typer.echo(json.dumps(payload, ensure_ascii=False))
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


@batch_app.command("show")
def batch_show(
    identifier: str,
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    settings = settings_option(config)
    try:
        with Session(require_engine(settings)) as session:
            typer.echo(json.dumps(batch_dict(get_batch(session, identifier)), ensure_ascii=False))
    except LookupError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@batch_app.command("freeze")
def batch_freeze(
    identifier: str,
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    settings = settings_option(config)
    try:
        with Session(require_engine(settings)) as session:
            batch = freeze_batch(session, identifier)
            session.commit()
            typer.echo(json.dumps(batch_dict(batch), ensure_ascii=False))
    except (LookupError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@batch_app.command("cancel")
def batch_cancel(
    identifier: str,
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    settings = settings_option(config)
    try:
        with Session(require_engine(settings)) as session:
            batch = cancel_batch(session, identifier)
            session.commit()
            typer.echo(json.dumps(batch_dict(batch), ensure_ascii=False))
    except (LookupError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@batch_app.command("discover")
def batch_discover(
    identifier: str,
    headed: bool = typer.Option(False, "--headed"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    settings = settings_option(config)
    engine = require_engine(settings)
    lock_path = settings.runtime_root / "discovery.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    discovery_lock = lock_path.open("a+")
    try:
        fcntl.flock(discovery_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        discovery_lock.close()
        typer.echo("another discovery process is already running", err=True)
        raise typer.Exit(2)
    with Session(engine) as session:
        try:
            batch = get_batch(session, identifier)
            if batch.frozen_at is None:
                raise ValueError("batch must be frozen before discovery")
            if batch.status in {BatchStatus.READY, BatchStatus.RUNNING, BatchStatus.PAUSED, BatchStatus.COMPLETED}:
                typer.echo(json.dumps(batch_dict(batch), ensure_ascii=False))
                discovery_lock.close()
                return
            limit = batch.planned_limit
            window_field = batch.window_field
            window_start = batch.window_start.replace(tzinfo=None) if batch.window_start else None
            window_end = batch.window_end.replace(tzinfo=None) if batch.window_end else None
        except (LookupError, ValueError) as exc:
            discovery_lock.close()
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
    previous_alarm_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, lambda _signum, _frame: (_ for _ in ()).throw(TimeoutError("discovery hard timeout exceeded")))
    signal.alarm(1800)
    try:
        with BrowserSession(settings, headed=headed) as browser:
            state = browser.login_with_saved_credentials(30)
            if state != LoginState.AUTHENTICATED:
                typer.echo("OA authentication required; rerun with --headed or run 'oa login'", err=True)
                raise typer.Exit(3)
            assert browser.page
            done_url = f"{browser.base_url}{settings.browser.done_list_path}"
            def in_window(item) -> bool:
                value = item.completed_at if window_field == "completed_at" else item.created_at
                return bool(value and (window_start is None or value >= window_start) and (window_end is None or value < window_end))

            max_pages = min(30, max(20, (limit + 19) // 20 + 5))
            discovery = DoneAdapter(browser.page, done_url).discover_pages(
                limit, accept=in_window, max_pages=max_pages,
                page_delay_seconds=settings.collector.list_page_delay_seconds,
                deal_time_start=window_start,
                deal_time_end=window_end,
            )
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_alarm_handler)
        discovery_lock.close()
    try:
        with Session(engine) as session:
            batch = apply_discovery(session, get_batch(session, identifier), discovery.items)
            batch.source_total_count = discovery.source_total_count
            batch.source_total_pages = discovery.source_total_pages
            batch.pages_scanned = discovery.pages_scanned
            batch.query_count = discovery.query_count
            batch.scanned_row_count = discovery.scanned_row_count
            session.commit()
            typer.echo(json.dumps(batch_dict(batch), ensure_ascii=False))
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@batch_app.command("archive-samples")
def batch_archive_samples(
    identifier: str,
    max_items: int = typer.Option(3, "--max-items", min=1, max=3),
    headed: bool = typer.Option(False, "--headed"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Archive at most three ordinary collaboration samples (stage 2A-2)."""
    settings = settings_option(config)
    engine = require_engine(settings)
    with Session(engine) as session:
        batch = get_batch(session, identifier)
        if batch.status not in {BatchStatus.READY, BatchStatus.RUNNING}:
            typer.echo("batch must be ready or running", err=True)
            raise typer.Exit(1)
        candidates = session.scalars(
            select(BatchItem)
            .where(BatchItem.batch_id == batch.id, BatchItem.archive_status == "pending")
            .order_by(BatchItem.ordinal)
            .limit(max_items)
        ).all()
        if not candidates:
            typer.echo(json.dumps(batch_dict(batch), ensure_ascii=False))
            return
        work = [(candidate.id, candidate.workitem_id_text) for candidate in candidates]

    results: list[dict[str, object]] = []
    with BrowserSession(settings, headed=headed) as browser:
        state = browser.login_with_saved_credentials(30)
        if state != LoginState.AUTHENTICATED:
            typer.echo("OA authentication required", err=True)
            raise typer.Exit(3)
        assert browser.page
        done_url = f"{browser.base_url}{settings.browser.done_list_path}"
        DoneAdapter(browser.page, done_url).open_list()
        adapter = CollaborationDetailAdapter(browser.page)
        for item_id, workitem_id in work:
            try:
                capture = adapter.capture(workitem_id)
                with Session(engine) as session:
                    item = session.get(BatchItem, item_id)
                    assert item is not None
                    manifest = archive_collaboration_detail(session, item, capture, settings.data_root)
                    batch = session.get(CollectionBatch, item.batch_id)
                    assert batch is not None
                    batch.status = BatchStatus.RUNNING
                    batch.archived_count = session.scalar(
                        select(func.count()).select_from(BatchItem).where(
                            BatchItem.batch_id == batch.id, BatchItem.archive_status == "archived"
                        )
                    ) or 0
                    session.commit()
                results.append({"workitem_id": workitem_id, "status": "archived", "files": sum(len(c.files) for c in manifest.containers), "attachments": len(capture.attachments)})
            except Exception as exc:
                with Session(engine) as session:
                    item = session.get(BatchItem, item_id)
                    if item is not None:
                        item.archive_status = "collect_failed"
                        item.retry_count += 1
                        item.last_error = f"{type(exc).__name__}: {exc}"[:1000]
                        session.commit()
                results.append({"workitem_id": workitem_id, "status": "collect_failed", "error": type(exc).__name__})
    typer.echo(json.dumps(results, ensure_ascii=False))
    if any(result["status"] != "archived" for result in results):
        raise typer.Exit(2)


@batch_app.command("archive-related")
def batch_archive_related(
    identifier: str,
    ordinal: int = typer.Option(..., "--ordinal", min=1),
    max_depth: int = typer.Option(10, "--max-depth", min=10, max=10),
    headed: bool = typer.Option(False, "--headed"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Re-capture one archived sample and follow every association up to depth 10."""
    settings = settings_option(config)
    engine = require_engine(settings)
    with Session(engine) as session:
        batch = get_batch(session, identifier)
        item = session.scalar(select(BatchItem).where(BatchItem.batch_id == batch.id, BatchItem.ordinal == ordinal))
        if item is None:
            typer.echo("batch item ordinal not found", err=True)
            raise typer.Exit(1)
        item_id, workitem_id = item.id, item.workitem_id_text
    try:
        with BrowserSession(settings, headed=headed) as browser:
            if browser.login_with_saved_credentials(30) != LoginState.AUTHENTICATED:
                raise RuntimeError("OA authentication required")
            assert browser.page
            DoneAdapter(browser.page, f"{browser.base_url}{settings.browser.done_list_path}").open_list()
            capture = CollaborationDetailAdapter(browser.page).capture(
                workitem_id, max_depth=max_depth,
                total_timeout_seconds=settings.collector.attachment_total_timeout_seconds,
                download_timeout_seconds=settings.collector.download_timeout_seconds,
            )
        with Session(engine) as session:
            item = session.get(BatchItem, item_id)
            assert item is not None
            manifest = archive_collaboration_detail(session, item, capture, settings.data_root)
            batch = session.get(CollectionBatch, item.batch_id)
            assert batch is not None
            batch.status = BatchStatus.RUNNING
            batch.archived_count = session.scalar(
                select(func.count()).select_from(BatchItem).where(
                    BatchItem.batch_id == batch.id, BatchItem.archive_status == "archived"
                )
            ) or 0
            session.commit()
        payload = {
            "workitem_id": workitem_id,
            "containers": len(manifest.containers),
            "max_depth": max(container.depth for container in manifest.containers),
            "files": sum(len(container.files) for container in manifest.containers),
            "verified_attachments": sum(
                1 for container in manifest.containers for file in container.files
                if file.file_role in MARKDOWN_SOURCE_ROLES and file.download_status == "verified"
            ),
            "depth_limit_reached": manifest.depth_limit_reached,
        }
        typer.echo(json.dumps(payload, ensure_ascii=False))
    except Exception as exc:
        with Session(engine) as session:
            item = session.get(BatchItem, item_id)
            if item is not None:
                item.retry_count += 1
                item.last_error = f"{type(exc).__name__}: {exc}"[:1000]
                session.commit()
        typer.echo(f"related archive failed: {type(exc).__name__}", err=True)
        raise typer.Exit(2) from exc


@batch_app.command("pause")
def batch_pause(
    identifier: str,
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    engine = require_engine(settings_option(config))
    try:
        with Session(engine) as session:
            batch = pause_batch(session, identifier)
            session.commit()
            typer.echo(json.dumps(batch_dict(batch), ensure_ascii=False))
    except (LookupError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@batch_app.command("resume")
def batch_resume(
    identifier: str,
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    engine = require_engine(settings_option(config))
    try:
        with Session(engine) as session:
            batch = resume_batch(session, identifier)
            session.commit()
            typer.echo(json.dumps(batch_dict(batch), ensure_ascii=False))
    except (LookupError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@batch_app.command("retry-item")
def batch_retry_item(
    identifier: str,
    ordinal: int = typer.Option(..., "--ordinal", min=1),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    engine = require_engine(settings_option(config))
    try:
        with Session(engine) as session:
            item = retry_batch_item(session, identifier, ordinal)
            session.commit()
            typer.echo(json.dumps({"ordinal": item.ordinal, "archive_status": item.archive_status}, ensure_ascii=False))
    except (LookupError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@batch_app.command("retry-failed")
def batch_retry_failed(
    identifier: str,
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    engine = require_engine(settings_option(config))
    try:
        with Session(engine) as session:
            count = retry_failed_items(session, identifier)
            session.commit()
            typer.echo(json.dumps({"retried": count}, ensure_ascii=False))
    except (LookupError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@batch_app.command("validate")
def batch_validate(
    identifier: str,
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Reconcile a batch: OA query count = manifest dedup count = archived + skipped + unresolved."""
    settings = settings_option(config)
    engine = require_engine(settings)
    try:
        with Session(engine) as session:
            report = validate_batch(session, identifier)
            payload = {
                "batch_key": report.batch_key,
                "discovered": report.discovered,
                "archived": report.archived,
                "failed": report.failed,
                "skipped": report.skipped,
                "reviewed": report.reviewed,
                "unresolved": report.unresolved,
                "reconciled": report.reconciled,
                "query_count": report.query_count,
                "source_match": report.source_match,
                "status": report.status,
            }
            typer.echo(json.dumps(payload, ensure_ascii=False))
            if not report.reconciled:
                raise typer.Exit(1)
            if not report.source_match:
                raise typer.Exit(2)
    except (LookupError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


@backfill_app.command("next")
def backfill_next(
    from_date: str = typer.Option("2019-01-01", "--from", help="Oldest inclusive completion date"),
    to_date: str = typer.Option("2026-01-01", "--to", help="Newest exclusive completion date"),
    headed: bool = typer.Option(False, "--headed"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Discover and freeze only the next gated month/half-month/week window."""
    settings = settings_option(config)
    engine = require_engine(settings)
    try:
        range_start, range_end = date.fromisoformat(from_date), date.fromisoformat(to_date)
        if range_start >= range_end:
            raise ValueError("backfill --from must be earlier than --to")
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc

    with Session(engine) as session:
        backfill_batches = session.scalars(
            select(CollectionBatch).where(CollectionBatch.notes.like("backfill:v1%"))
            .order_by(CollectionBatch.window_start.desc())
        ).all()
        active = next((batch for batch in backfill_batches if batch.status != BatchStatus.COMPLETED), None)
        if active is not None:
            typer.echo(json.dumps({"created": False, "reason": "current_batch_not_completed", "batch": batch_dict(active)}, ensure_ascii=False))
            raise typer.Exit(1)
        cursor_end = min(
            (batch.window_start.date() for batch in backfill_batches if batch.window_start),
            default=range_end,
        )
    window = next_month_remainder(range_start, cursor_end)
    if window is None:
        typer.echo(json.dumps({"status": "complete", "cursor": cursor_end.isoformat()}, ensure_ascii=False))
        return

    zone = ZoneInfo(settings.app.timezone)
    with BrowserSession(settings, headed=headed) as browser:
        if browser.login_with_saved_credentials(30) != LoginState.AUTHENTICATED:
            typer.echo("OA authentication required", err=True)
            raise typer.Exit(3)
        assert browser.page
        adapter = DoneAdapter(browser.page, f"{browser.base_url}{settings.browser.done_list_path}")
        while True:
            start = datetime.combine(window.start, time.min)
            end = datetime.combine(window.end, time.min)
            discovery = adapter.discover_pages(
                501, max_pages=500,
                page_delay_seconds=settings.collector.list_page_delay_seconds,
                deal_time_start=start, deal_time_end=end,
            )
            if len(discovery.items) <= 500:
                break
            window = shrink_latest(window)

    with Session(engine) as session:
        start_aware = datetime.combine(window.start, time.min, zone)
        end_aware = datetime.combine(window.end, time.min, zone)
        notes = f"backfill:v1;granularity={window.granularity};range={from_date}:{to_date}"
        batch, created = plan_batch(session, BatchPlan(
            "done", start_aware, end_aware, "completed_at", max(1, len(discovery.items)), notes,
        ))
        if not created:
            typer.echo(json.dumps({"created": False, "batch": batch_dict(batch)}, ensure_ascii=False))
            return
        freeze_batch(session, batch.batch_key)
        batch = apply_discovery(session, batch, discovery.items)
        batch.source_total_count = discovery.source_total_count
        batch.source_total_pages = discovery.source_total_pages
        batch.pages_scanned = discovery.pages_scanned
        batch.query_count = discovery.query_count
        batch.scanned_row_count = discovery.scanned_row_count
        apply_business_exclusions(session, batch.id, settings.collector.excluded_title_patterns)
        from oa_knowledge.web.status import _apply_policy_to_pending
        for policy in session.scalars(select(ExclusionPolicy).where(ExclusionPolicy.enabled.is_(True))).all():
            _apply_policy_to_pending(session, policy)
        if batch.discovered_count == 0:
            batch.status = BatchStatus.COMPLETED
            batch.finished_at = datetime.now(timezone.utc)
        session.commit()
        typer.echo(json.dumps({
            "created": True, "granularity": window.granularity,
            "window": {"start": window.start.isoformat(), "end": window.end.isoformat()},
            "batch": batch_dict(batch),
        }, ensure_ascii=False))


@backfill_app.command("start")
def backfill_start(
    from_date: str = typer.Option("2019-01-01", "--from"),
    to_date: str = typer.Option("2026-01-01", "--to"),
    chunk_size: int = typer.Option(20, "--chunk-size", min=1, max=20),
    time_budget_seconds: int = typer.Option(1800, "--time-budget-seconds", min=60, max=1800),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Queue a restart-safe campaign that completes every gated historical window."""
    from oa_knowledge.web.status import start_backfill_campaign

    try:
        payload = start_backfill_campaign(
            settings_option(config), from_date, to_date, chunk_size, time_budget_seconds,
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    typer.echo(json.dumps(payload, ensure_ascii=False))


@batch_app.command("run")
def batch_run(
    identifier: str,
    max_items: int = typer.Option(20, "--max-items", min=1, max=100),
    time_budget_seconds: int = typer.Option(900, "--time-budget-seconds", min=10, max=3600),
    headed: bool = typer.Option(False, "--headed"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
    operation_job_id: int | None = typer.Option(None, "--operation-job-id", hidden=True),
) -> None:
    """Run or continue a bounded Pilot-20 batch, committing after every item."""
    settings = settings_option(config)
    engine = require_engine(settings)
    run_key = f"pilot20-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid4().hex[:8]}"
    with Session(engine) as session:
        for stale_run in session.scalars(select(Run).where(Run.status == "running")).all():
            stale_run.status = "interrupted"
            stale_run.finished_at = datetime.now(timezone.utc)
            stale_run.summary_json = json.dumps({"recovered_by": run_key})
        batch = get_batch(session, identifier)
        if batch.status == BatchStatus.PAUSED:
            typer.echo("batch is paused; run 'oa batch resume' first", err=True)
            raise typer.Exit(1)
        if batch.status == BatchStatus.READY:
            batch.status = BatchStatus.RUNNING
        elif batch.status not in {BatchStatus.RUNNING, BatchStatus.FAILED}:
            typer.echo(f"batch cannot run from status: {batch.status}", err=True)
            raise typer.Exit(1)
        if batch.status == BatchStatus.FAILED:
            batch.status = BatchStatus.RUNNING
        excluded = apply_business_exclusions(session, batch.id, settings.collector.excluded_title_patterns)
        reused = reuse_verified_items(session, batch.id)
        recover_interrupted_items(session, batch.id)
        run = Run(run_key=run_key, stage="2A-4", status="running")
        session.add(run)
        session.commit()
        batch_id, run_id = batch.id, run.id
        batch_window_start = batch.window_start.replace(tzinfo=None) if batch.window_start else None
        batch_window_end = batch.window_end.replace(tzinfo=None) if batch.window_end else None

    deadline = monotonic() + time_budget_seconds
    processed = 0
    outcome = "paused"
    interruption_error: str | None = None
    heartbeat_stop = Event()
    heartbeat_thread: Thread | None = None
    if operation_job_id is not None:
        heartbeat_thread = Thread(
            target=_operation_heartbeat_loop,
            args=(engine, operation_job_id, heartbeat_stop),
            daemon=True,
            name=f"operation-heartbeat-{operation_job_id}",
        )
        heartbeat_thread.start()
    try:
        with BrowserSession(settings, headed=headed) as browser:
            if browser.login_with_saved_credentials(30) != LoginState.AUTHENTICATED:
                outcome = "auth_required"
                raise RuntimeError("OA authentication required")
            assert browser.page
            list_adapter = DoneAdapter(browser.page, f"{browser.base_url}{settings.browser.done_list_path}")
            list_frame = list_adapter.open_list()
            if batch_window_start is not None and batch_window_end is not None:
                list_adapter.apply_deal_time_filter(list_frame, batch_window_start, batch_window_end)
            adapter = CollaborationDetailAdapter(
                browser.page,
                attachment_resolver=verified_attachment_resolver(engine, settings.data_root),
            )
            current_list_page = 1
            while processed < max_items and monotonic() < deadline:
                item_failed = False
                with Session(engine) as session:
                    batch = session.get(CollectionBatch, batch_id)
                    assert batch is not None
                    if batch.status == BatchStatus.PAUSED:
                        outcome = "paused"
                        break
                    item = session.scalar(
                        select(BatchItem).where(
                            BatchItem.batch_id == batch_id, BatchItem.archive_status == "pending"
                        ).order_by(BatchItem.ordinal).limit(1)
                    )
                    if item is None:
                        outcome = "drained"
                        break
                    item.archive_status = "archiving"
                    item_id, workitem_id, list_page = item.id, item.workitem_id_text, item.list_page or 1
                    session.commit()
                try:
                    if list_page != current_list_page:
                        list_adapter.navigate_to_page(list_page, settings.collector.list_page_delay_seconds)
                        current_list_page = list_page
                    capture = adapter.capture(
                        workitem_id, max_depth=10,
                        total_timeout_seconds=settings.collector.attachment_total_timeout_seconds,
                        download_timeout_seconds=settings.collector.download_timeout_seconds,
                    )
                    with Session(engine) as session:
                        item = session.get(BatchItem, item_id)
                        assert item is not None
                        archive_collaboration_detail(session, item, capture, settings.data_root)
                        if item.archive_status == "download_failed":
                            terminal_attachment_failure = any(
                                attachment.download_status == "known_download_failed"
                                for attachment in capture.attachments
                            ) or any(
                                attachment.download_status == "known_download_failed"
                                for container in capture.related_containers
                                for attachment in container.attachments
                            )
                            item.retry_count = max(item.retry_count + 1, 2 if terminal_attachment_failure else 1)
                            item.last_error = "attachment_verification_failed"
                            item_failed = True
                            outcome = "item_failed"
                        session.commit()
                except Exception as exc:
                    with Session(engine) as session:
                        item = session.get(BatchItem, item_id)
                        if item is not None:
                            item.archive_status = "collect_failed"
                            item.retry_count += 1
                            item.last_error = _sanitize_operational_error(exc)
                            session.commit()
                    item_failed = True
                    outcome = "item_failed"
                processed += 1
                if operation_job_id is not None:
                    with Session(engine) as session:
                        operation_job = session.get(OperationJob, operation_job_id)
                        if operation_job is not None:
                            now = datetime.now(timezone.utc)
                            if operation_job.job_type != "backfill_campaign":
                                operation_job.progress_current = processed
                            operation_job.heartbeat_at = now
                            operation_job.lease_expires_at = now + LEASE_TTL
                            session.commit()
                if item_failed:
                    break
                browser.page.wait_for_timeout(int(settings.collector.item_delay_seconds * 1000))
            if monotonic() >= deadline:
                outcome = "budget_exhausted"
    except Exception as exc:
        interruption_error = f"{type(exc).__name__}: {exc}"[:1000]
        if outcome != "auth_required":
            outcome = "interrupted"
    finally:
        heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=2)
        with Session(engine) as session:
            batch = session.get(CollectionBatch, batch_id)
            run = session.get(Run, run_id)
            assert batch is not None and run is not None
            batch.archived_count = session.scalar(select(func.count()).select_from(BatchItem).where(BatchItem.batch_id == batch_id, BatchItem.archive_status == "archived")) or 0
            active_failed = session.scalar(select(func.count()).select_from(BatchItem).where(BatchItem.batch_id == batch_id, BatchItem.archive_status.in_({"collect_failed", "download_failed"}))) or 0
            review_required = session.scalar(select(func.count()).select_from(BatchItem).where(BatchItem.batch_id == batch_id, BatchItem.archive_status == "review_required")) or 0
            batch.failed_count = active_failed + review_required
            batch.skipped_count = session.scalar(select(func.count()).select_from(BatchItem).where(BatchItem.batch_id == batch_id, BatchItem.archive_status == "confirmed_skip")) or 0
            pending = session.scalar(select(func.count()).select_from(BatchItem).where(BatchItem.batch_id == batch_id, BatchItem.archive_status.in_({"pending", "archiving"}))) or 0
            if batch.archived_count + batch.skipped_count + review_required == batch.discovered_count:
                batch.status = BatchStatus.COMPLETED
                batch.finished_at = datetime.now(timezone.utc)
                outcome = "completed_with_issues" if review_required else "completed"
            else:
                batch.status = BatchStatus.PAUSED
            run.status = outcome
            run.finished_at = datetime.now(timezone.utc)
            run.summary_json = json.dumps({"processed": processed, "reused": reused, "excluded": excluded, "pending": pending, "failed": active_failed, "review_required": review_required, "interruption_error": interruption_error})
            payload = batch_dict(batch) | {"run_key": run_key, "run_status": outcome, "processed": processed, "reused": reused, "excluded": excluded, "interruption_error": interruption_error}
            session.commit()
    typer.echo(json.dumps(payload, ensure_ascii=False))
    if outcome in {"auth_required", "interrupted"}:
        raise typer.Exit(2)


def _sanitize_operational_error(exc: Exception) -> str:
    value = f"{type(exc).__name__}: {exc}"
    value = re.sub(r"(?im)^\s*-\s*(?:cookie|authorization|proxy-authorization):.*$", "  - [credential header redacted]", value)
    value = re.sub(r"(?i)(?:JSESSIONID|token|password|passwd|secret)=([^;&\s]+)", lambda m: m.group(0).split("=", 1)[0] + "=[redacted]", value)
    return value[:1000]


def _attachment_failure_summary(capture) -> str:
    failures = [
        str(issue.get("error")) for issue in capture.capture_issues
        if issue.get("kind") == "attachment_download_failed" and issue.get("error")
    ]
    return "; ".join(dict.fromkeys(failures))[:1000] or "attachment verification failed"


def _audit_opens_detail(processing_status: str) -> bool:
    """A full audit reopens every item not skipped by the latest policy set."""
    return processing_status != "skipped"


def _operation_heartbeat_loop(engine, job_id: int, stop: Event) -> None:
    """Keep a long single-item capture lease live without changing its progress."""
    while not stop.is_set():
        try:
            with Session(engine) as session:
                job = session.get(OperationJob, job_id)
                if job is None or job.status not in {"queued", "running"}:
                    return
                now = datetime.now(timezone.utc)
                job.heartbeat_at = now
                job.lease_expires_at = now + LEASE_TTL
                session.commit()
        except Exception:
            # The item transaction is authoritative; a transient SQLite lock is retried.
            pass
        stop.wait(15)


@parse_app.command("file")
def parse_file_cmd(
    file_id: int = typer.Argument(..., help="File ID to parse"),
    engine: str | None = typer.Option(None, "--engine", help="Force parser engine (markitdown/mineru)"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Parse a single verified file."""
    settings = settings_option(config)
    engine_obj = require_engine(settings)
    from oa_knowledge.pipeline import ParsePipeline

    pipeline = ParsePipeline(settings, engine_obj)
    job_id = pipeline.enqueue(file_id, engine=engine)
    if job_id is None:
        typer.echo(f"file_id={file_id}: no verified file or enqueue failed", err=True)
        raise typer.Exit(1)
    result = pipeline.run(job_id)
    typer.echo(json.dumps({
        "job_id": job_id,
        "engine": result.engine,
        "output": str(result.output_path),
        "quality_score": result.quality_score,
        "warnings": result.warnings,
        "text_length": result.text_length,
    }, ensure_ascii=False))


@parse_app.command("pending")
def parse_pending(
    limit: int = typer.Option(50, "--limit", min=1, max=500, help="Max jobs to process"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Process all queued parse jobs."""
    settings = settings_option(config)
    engine_obj = require_engine(settings)
    from oa_knowledge.pipeline import ParsePipeline

    pipeline = ParsePipeline(settings, engine_obj)
    summary = pipeline.run_all_pending(limit=limit)
    typer.echo(json.dumps(summary, ensure_ascii=False))
    if summary["failed"] > 0:
        raise typer.Exit(1)


@parse_app.command("tasks")
def parse_tasks(
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Extract tasks from classified items."""
    settings = settings_option(config)
    engine_obj = require_engine(settings)
    from oa_knowledge.digest.tasks import TaskExtractor

    extractor = TaskExtractor(settings, engine_obj)
    summary = extractor.extract_all_pending(limit=50)
    typer.echo(json.dumps(summary, ensure_ascii=False))


@parse_app.command("digest")
def parse_digest(
    date_str: str = typer.Option("today", "--date", help="Date for digest (YYYY-MM-DD or 'today')"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Generate a local daily digest preview."""
    settings = settings_option(config)
    engine_obj = require_engine(settings)

    if date_str == "today":
        date_str = datetime.now().strftime("%Y-%m-%d")

    with Session(engine_obj) as session:
        from oa_knowledge.db.models import Task as TaskModel

        total = session.scalar(select(func.count()).select_from(TaskModel)) or 0
        candidates = session.scalar(
            select(func.count()).select_from(TaskModel).where(TaskModel.status == "candidate")
        ) or 0
        confirmed = session.scalar(
            select(func.count()).select_from(TaskModel).where(TaskModel.status == "confirmed")
        ) or 0

    typer.echo(json.dumps({
        "date": date_str,
        "total_tasks": total,
        "candidates": candidates,
        "confirmed": confirmed,
        "preview": f"OA 每日摘要｜{date_str}\n总计 {total} 项任务，{candidates} 候选，{confirmed} 已确认",
    }, ensure_ascii=False))


wiki_app = typer.Typer(help="Stage 6 LLM Wiki ingestion and lint")
app.add_typer(wiki_app, name="wiki", hidden=True)


@wiki_app.command("trial-publish")
def wiki_trial_publish(
    limit: int = typer.Option(10, "--limit", min=1, max=100, help="Maximum OA items"),
    clear: bool = typer.Option(False, "--clear", help="Rebuild the generated Vault projection"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Publish an attachment-first, model-classified Vault trial."""
    settings = settings_option(config)
    engine_obj = require_engine(settings)
    from oa_knowledge.knowledge_trial import (
        prepare_trial_parses, run_item_first_trial, select_trial_item_ids,
    )

    item_ids = select_trial_item_ids(engine_obj, limit)
    parse_summary = prepare_trial_parses(settings, engine_obj, item_ids)
    result = run_item_first_trial(settings, engine_obj, item_ids=item_ids, clear=clear)
    result["parse_summary"] = parse_summary
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@wiki_app.command("ingest")
def wiki_ingest(
    pending: bool = typer.Option(False, "--pending", help="Ingest all stale/unprocessed source notes"),
    limit: int = typer.Option(20, "--limit", min=1, max=500, help="Max notes to ingest"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Incrementally ingest source notes into the LLM Wiki."""
    settings = settings_option(config)
    vault_root = settings.data_root / "vault"
    from oa_knowledge.wiki.ingest import WikiIngestor

    ingestor = WikiIngestor(vault_root)
    if pending:
        summary = ingestor.ingest_stale(limit=limit)
    else:
        # Ingest all source notes
        summary = {"ingested": 0, "skipped": 0, "failed": 0, "errors": []}
        if ingestor.raw_sources.is_dir():
            for source_md in ingestor.raw_sources.rglob("source.md"):
                result = ingestor.ingest_single(source_md)
                if result:
                    summary["ingested"] += 1
                else:
                    summary["skipped"] += 1
    typer.echo(json.dumps(summary, ensure_ascii=False))


@wiki_app.command("lint")
def wiki_lint(
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Run lint checks on Wiki and source notes."""
    settings = settings_option(config)
    vault_root = settings.data_root / "vault"
    from oa_knowledge.wiki.lint import WikiLinter

    linter = WikiLinter(vault_root)
    issues = linter.lint()

    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]
    infos = [i for i in issues if i.severity == "info"]

    typer.echo(json.dumps({
        "total": len(issues),
        "errors": len(errors),
        "warnings": len(warnings),
        "infos": len(infos),
        "issues": [
            {"severity": i.severity, "file": i.file, "message": i.message}
            for i in issues
        ],
    }, ensure_ascii=False))

    if errors:
        raise typer.Exit(1)


@archive_app.command("migrate-paths")
def migrate_paths(
    dry_run: bool = typer.Option(True, "--dry-run/--yes", help="Preview changes (default) or apply them"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Unify legacy ``raw/done`` and ``raw/pending`` archives under ``archive/raw/oa``.

    Defaults to a dry run. Pass ``--yes`` to apply. STOP the OA worker and
    markdown-worker and BACK UP ``oa.db`` first — this moves directories on disk
    and rewrites stored relative paths. The operation is idempotent: items
    already under the unified prefix are skipped.
    """
    settings = settings_option(config)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            counts = migrate_archive_paths(session, settings, dry_run=dry_run)
    finally:
        engine.dispose()
    typer.echo(json.dumps(counts, ensure_ascii=False))
    if dry_run and counts["migrated"]:
        typer.echo("Above items would be migrated. Re-run with --yes after backing up the database.", err=True)




# --------------------------------------------------------------------------- #
# Scheduled sync orchestration (plan-0806-1 §2.3 / §4)
# --------------------------------------------------------------------------- #
# The actual scan logic lives in ``oa_knowledge.scheduled_sync`` so the CLI and
# the worker daemon share one implementation. These commands only set up the
# engine/config and delegate.
@schedule_app.command("bootstrap")
def schedule_bootstrap(
    headed: bool = typer.Option(False, "--headed"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """First-deploy seeding: discover Pending without notifying and sync the full Done manifest.

    Run this once before enabling the hourly/nightly timers so the existing
    backlog is archived and summarized but NOT pushed to Feishu (plan-0805-02 §1.2).
    """
    settings = settings_option(config)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    result = run_bootstrap_scan(engine, settings, headed=headed)
    engine.dispose()
    typer.echo(json.dumps(result, ensure_ascii=False))


@schedule_app.command("hourly")
def schedule_hourly(
    headed: bool = typer.Option(False, "--headed"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Hourly working-hours scan: full Pending snapshot (notify) + Done known-boundary incremental."""
    settings = settings_option(config)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    result = run_hourly_scan(engine, settings, headed=headed)
    engine.dispose()
    typer.echo(json.dumps(result, ensure_ascii=False))


@schedule_app.command("nightly")
def schedule_nightly(
    headed: bool = typer.Option(False, "--headed"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Nightly full sync: complete Done manifest, enqueue all pending downloads, recover stuck tasks."""
    settings = settings_option(config)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    result = run_nightly_scan(engine, settings, headed=headed)
    engine.dispose()
    typer.echo(json.dumps(result, ensure_ascii=False))


@schedule_app.command("enqueue")
def schedule_enqueue(
    stage: str = typer.Argument(..., help="hourly 或 nightly"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """只创建持久调度任务，由唯一 OA Worker 串行使用浏览器登录态。"""
    settings = settings_option(config)
    upgrade_database(settings.database_path)
    from oa_knowledge.web.schedule_views import trigger_schedule_run
    try:
        result = trigger_schedule_run(settings, stage, config_path=config)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="stage") from exc
    typer.echo(json.dumps(result, ensure_ascii=False))


@schedule_app.command("status")
def schedule_status(
    limit: int = typer.Option(10, "--limit", min=1, max=100),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Show recent scheduled run records from the ``runs`` table."""
    settings = settings_option(config)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            rows = session.scalars(
                select(Run).where(Run.stage.in_(("scheduled_bootstrap", "scheduled_hourly", "scheduled_nightly")))
                .order_by(Run.id.desc()).limit(limit)
            ).all()
            typer.echo(json.dumps([{
                "run_key": r.run_key, "stage": r.stage, "status": r.status,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "summary": json.loads(r.summary_json or "{}"),
            } for r in rows], ensure_ascii=False))
    finally:
        engine.dispose()


@notifications_app.command("test-feishu")
def notifications_test_feishu(
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Send the synthetic connectivity-test card (no real OA data).

    Exits non-zero when Feishu is not ready or the send fails.
    """
    settings = settings_option(config)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    try:
        state = validate_feishu_runtime_config(settings)
        if state != "ready":
            typer.echo(f"feishu not ready: {state}", err=True)
            raise typer.Exit(1)
        from oa_knowledge.notifications.feishu_service import FeishuService
        result = FeishuService(settings).send_test()
        typer.echo(json.dumps({
            "status": result.status,
            "retryable": result.retryable,
            "error_code": result.error_code,
        }, ensure_ascii=False))
        if result.status != "sent":
            raise typer.Exit(1)
    finally:
        engine.dispose()


@notifications_app.command("status")
def notifications_status(
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Show Feishu delivery health: recent send/fail times, status counts, latest error."""
    from oa_knowledge.db.models import NotificationDelivery

    settings = settings_option(config)
    upgrade_database(settings.database_path)
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
        state = validate_feishu_runtime_config(settings)
        typer.echo(json.dumps({
            "feishu_state": state,
            "last_success_at": latest_sent.isoformat() if latest_sent else None,
            "last_error_code": latest_error.error_code if latest_error else None,
            "last_error_at": latest_error.updated_at.isoformat() if latest_error else None,
            "counts": counts,
        }, ensure_ascii=False))
    finally:
        engine.dispose()


@notifications_app.command("retry")
def notifications_retry(
    delivery_id: int = typer.Argument(..., help="NotificationDelivery id to re-send"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Re-send a parked pending_summary delivery by id (manual retry)."""
    settings = settings_option(config)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    try:
        from oa_knowledge.notifications.feishu_service import retry_pending_summary_delivery
        result = retry_pending_summary_delivery(engine, settings, delivery_id)
        typer.echo(json.dumps({
            "status": result.status,
            "retryable": result.retryable,
            "error_code": result.error_code,
        }, ensure_ascii=False))
        if result.status != "sent":
            raise typer.Exit(1)
    finally:
        engine.dispose()


@knowledge_app.command("audit-handoff")
def knowledge_audit_handoff(
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """Audit the Done-archive → Markdown knowledge handoff (plan-0805-02 §4.4)."""
    settings = settings_option(config)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    try:
        from oa_knowledge.markdown_queue import audit_handoff
        report = audit_handoff(engine, settings)
        typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()


@curate_app.command("plan")
def curate_plan(
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
    limit: int | None = typer.Option(None, "--limit", min=1, max=100),
    oa_item_key: str | None = typer.Option(None, "--oa-item-key"),
) -> None:
    """Read-only package/rule inventory. Does not call the model or write Curated state."""
    from oa_knowledge.curation.service import plan_curation
    settings = settings_option(config)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    try:
        typer.echo(json.dumps(plan_curation(settings, engine, limit=limit, oa_item_key=oa_item_key).model_dump(), ensure_ascii=False))
    finally:
        engine.dispose()


@curate_app.command("run")
def curate_run(
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
    limit: int | None = typer.Option(None, "--limit", min=1, max=100),
    oa_item_key: str | None = typer.Option(None, "--oa-item-key"),
) -> None:
    """Run a bounded local-model curation batch."""
    from oa_knowledge.curation.service import run_curation
    settings = settings_option(config)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    try:
        result = run_curation(settings, engine, limit=limit, oa_item_key=oa_item_key)
        typer.echo(json.dumps(result.model_dump(), ensure_ascii=False))
        if result.failed:
            raise typer.Exit(1)
    finally:
        engine.dispose()


@curate_app.command("retry")
def curate_retry(
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
    limit: int = typer.Option(10, "--limit", min=1, max=100),
) -> None:
    """Retry a bounded batch; completed signatures remain skipped."""
    curate_run(config=config, limit=limit, oa_item_key=None)


@curate_app.command("validate")
def curate_validate(
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    from oa_knowledge.curation.service import validate_curation
    settings = settings_option(config)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    try:
        result = validate_curation(settings, engine)
        typer.echo(json.dumps(result, ensure_ascii=False))
        if not result["ok"]:
            raise typer.Exit(1)
    finally:
        engine.dispose()


@curate_app.command("report")
def curate_report(
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    from oa_knowledge.curation.service import curation_report
    settings = settings_option(config)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    try:
        typer.echo(json.dumps(curation_report(engine), ensure_ascii=False))
    finally:
        engine.dispose()


@data_app.command("status")
def data_status(
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """查看清理运行汇总；不输出候选文件名或 OA 内容。"""
    from oa_knowledge.web.data_governance_views import data_governance_view
    settings = settings_option(config)
    engine = require_engine(settings)
    engine.dispose()
    typer.echo(json.dumps(data_governance_view(settings), ensure_ascii=False))


@data_app.command("plan")
def data_plan(
    categories: str = typer.Option(
        "browser_cache,runtime_reports,expired_backups,sent_pending_orphans",
        "--categories",
    ),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """只读扫描并持久化清理候选；不会移动或删除文件。"""
    from oa_knowledge.data_governance.service import build_cleanup_plan
    settings = settings_option(config)
    engine = require_engine(settings)
    try:
        selected = {value.strip() for value in categories.split(",") if value.strip()}
        result = build_cleanup_plan(settings, engine, categories=selected)
        typer.echo(json.dumps(asdict(result), ensure_ascii=False))
    finally:
        engine.dispose()


@data_app.command("quarantine")
def data_quarantine(
    run_id: int,
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """按预检清单把文件原子移动到同盘隔离区。"""
    from oa_knowledge.data_governance.quarantine import quarantine_run
    settings = settings_option(config)
    engine = require_engine(settings)
    try:
        typer.echo(json.dumps(asdict(quarantine_run(settings, engine, run_id)), ensure_ascii=False))
    finally:
        engine.dispose()


@data_app.command("restore")
def data_restore(
    run_id: int,
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """恢复一个隔离运行；绝不覆盖已重建的目标文件。"""
    from oa_knowledge.data_governance.quarantine import restore_run
    settings = settings_option(config)
    engine = require_engine(settings)
    try:
        typer.echo(json.dumps(asdict(restore_run(settings, engine, run_id)), ensure_ascii=False))
    finally:
        engine.dispose()


@data_app.command("purge")
def data_purge(
    run_id: int,
    confirmation: str = typer.Option(..., "--confirmation"),
    config: Path | None = typer.Option(None, "--config", exists=True, dir_okay=False),
) -> None:
    """隔离期满后以精确确认串永久清除隔离文件。"""
    from oa_knowledge.data_governance.quarantine import purge_run
    settings = settings_option(config)
    engine = require_engine(settings)
    try:
        result = purge_run(settings, engine, run_id, confirmation=confirmation)
        typer.echo(json.dumps(asdict(result), ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    app()
