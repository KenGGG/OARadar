"""Durable scheduled sync orchestration (plan-0805-02 §2).

The ``scripts/hourly-sync.sh`` entry point still exists, but the actual
orchestration lives here so every run is recorded in the ``runs`` table, the
Pending scan is a complete snapshot, and the Done scan uses a ``known-boundary``
algorithm instead of a fixed page count. The browser/CLI layer calls these
functions; the pure helpers are unit-tested without any OA access.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol, Sequence
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.collector import BrowserSession, DoneAdapter, LoginState
from oa_knowledge.collector.pending import PENDING_LIST_PATH, PendingAdapter
from oa_knowledge.db.models import OAManifestItem, PipelineTask, Run
from oa_knowledge.full_manifest import synchronize_manifest
from oa_knowledge.markdown_queue import enqueue_missing_markdown_tasks
from oa_knowledge.pending_sync import PendingSyncResult, sync_pending_discovery
from oa_knowledge.production_pipeline import QUEUE_PRIORITY
from oa_knowledge.resources import ResourceCoordinator


# --------------------------------------------------------------------------- #
# Run ledger
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    return datetime.now(timezone.utc)


def record_scheduled_run(session: Session, stage: str, *, run_key: str | None = None) -> Run:
    """Insert a ``running`` Run row and return it.

    ``stage`` is one of ``scheduled_bootstrap`` / ``scheduled_hourly`` /
    ``scheduled_nightly`` (plan-0805-02 §2.1).
    """
    run = Run(
        run_key=run_key or f"{stage}:{uuid4().hex}",
        stage=stage,
        status="running",
        summary_json=json.dumps(
            {"started_at": _now().isoformat(), "status": "running"}, ensure_ascii=False
        ),
    )
    session.add(run)
    session.flush()
    return run


def close_scheduled_run(
    session: Session,
    run: Run,
    status: str,
    *,
    pending: dict | None = None,
    done: dict | None = None,
) -> None:
    """Finalize a Run row with the structured summary (plan-0805-02 §2.1)."""
    summary = {
        "pending": pending or {},
        "done": done or {},
        "started_at": json.loads(run.summary_json or "{}").get("started_at"),
        "finished_at": _now().isoformat(),
        "status": status,
    }
    run.status = status
    run.finished_at = _now()
    run.summary_json = json.dumps(summary, ensure_ascii=False)
    session.flush()


# --------------------------------------------------------------------------- #
# Pending full snapshot
# --------------------------------------------------------------------------- #
class PendingAdapterProtocol(Protocol):
    def discover_all_pages(self, page_delay_seconds: float = 0):
        """Return a discovery object with ``.items`` (iterable) and ``.source_total_pages``."""


def run_pending_scan(
    session: Session,
    adapter: PendingAdapterProtocol,
    *,
    notification_mode: str = "normal",
    page_delay_seconds: float = 0,
) -> tuple[PendingSyncResult, dict]:
    """Scan the entire Pending list and sync it idempotently.

    Uses a full snapshot (not a fixed ``limit``) so locally-missing items are
    only closed when the snapshot is complete (plan-0805-02 §2.2).
    """
    discovery = adapter.discover_all_pages(page_delay_seconds=page_delay_seconds)
    complete_snapshot = (
        discovery.source_total_pages is not None
        and discovery.pages_scanned >= discovery.source_total_pages
    )
    result = sync_pending_discovery(
        session,
        list(discovery.items),
        authoritative=complete_snapshot,
        notification_mode=notification_mode,
    )
    summary = {
        "source_total": discovery.source_total_count,
        "pages_scanned": discovery.pages_scanned,
        "created": result.created,
        "updated": result.updated,
        "unchanged": result.unchanged,
        "closed": result.closed,
        "tasks_enqueued": result.created + result.updated,
    }
    return result, summary


# --------------------------------------------------------------------------- #
# Done known-boundary incremental scan
# --------------------------------------------------------------------------- #
class DonePageLike(Protocol):
    @property
    def items(self) -> Sequence: ...
    @property
    def is_last_page(self) -> bool: ...


@dataclass(frozen=True)
class DoneBoundaryResult:
    new_items: list = field(default_factory=list)
    pages_scanned: int = 0
    reached_boundary: bool = False
    reached_max_pages: bool = False
    source_total_pages: int | None = None


def scan_done_known_boundary(
    page_source: Callable[[int], DonePageLike],
    *,
    is_known_unchanged: Callable[[object], bool],
    known_boundary_count: int = 20,
    max_pages: int = 20,
) -> DoneBoundaryResult:
    """Walk Done-list pages and stop at a stable known boundary (plan-0805-02 §2.3).

    Stop conditions (first match wins):

    1. OA's last page is reached;
    2. ``known_boundary_count`` consecutive items are known and unchanged;
    3. ``max_pages`` safety ceiling is hit (the caller should treat this as a
       partial scan and let the nightly full sync catch up).
    """
    new_items: list = []
    consecutive_known = 0
    pages_scanned = 0
    source_total_pages: int | None = None
    for page_number in range(1, max_pages + 1):
        page = page_source(page_number)
        pages_scanned += 1
        source_total_pages = getattr(page, "source_total_pages", None)
        for item in page.items:
            if is_known_unchanged(item):
                consecutive_known += 1
                if consecutive_known >= known_boundary_count:
                    return DoneBoundaryResult(
                        new_items=new_items, pages_scanned=pages_scanned,
                        reached_boundary=True, source_total_pages=source_total_pages,
                    )
            else:
                consecutive_known = 0
                new_items.append(item)
        if page.is_last_page:
            return DoneBoundaryResult(
                new_items=new_items, pages_scanned=pages_scanned,
                reached_boundary=False, source_total_pages=source_total_pages,
            )
    return DoneBoundaryResult(
        new_items=new_items, pages_scanned=pages_scanned,
        reached_boundary=False, reached_max_pages=True, source_total_pages=source_total_pages,
    )


# --------------------------------------------------------------------------- #
# Manifest item idempotent upsert
# --------------------------------------------------------------------------- #
def ensure_manifest_item(session: Session, oa_item_key: str, *, title: str | None = None) -> OAManifestItem:
    """Idempotently upsert a minimal OAManifestItem so the download task resolves.

    The full ``synchronize_manifest`` pass (bootstrap/nightly) fills in the
    remaining discovery fields; this only guarantees a resolvable row exists
    for a newly discovered hourly item.
    """
    row = session.scalar(select(OAManifestItem).where(OAManifestItem.oa_item_key == oa_item_key))
    if row is None:
        workitem_id = oa_item_key[len("done:"):] if oa_item_key.startswith("done:") else oa_item_key
        row = OAManifestItem(
            oa_item_key=oa_item_key,
            workitem_id_text=workitem_id,
            title=title or oa_item_key,
            list_page=0,
        )
        session.add(row)
        session.flush()
    elif title and not row.title:
        row.title = title
    return row


# --------------------------------------------------------------------------- #
# Done discovery hashing (plan-0806-1 §3.4 idempotency)
# --------------------------------------------------------------------------- #
def _done_signature(item) -> str:
    import hashlib

    payload = "|".join((
        item.title or "",
        item.sender or "",
        item.created_at.isoformat() if getattr(item, "created_at", None) else "",
        item.completed_at.isoformat() if getattr(item, "completed_at", None) else "",
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _load_known_done_hashes(engine) -> dict[str, str | None]:
    """Preload known Done items and their discovery hashes in a single query."""
    with Session(engine) as session:
        rows = session.execute(
            select(OAManifestItem.oa_item_key, OAManifestItem.discovery_hash)
        ).all()
    return {key: value for key, value in rows}


def _is_known_unchanged(known: dict[str, str | None], item) -> bool:
    stored = known.get(item.oa_item_key)
    if stored is None:
        return False
    return stored == _done_signature(item)


# --------------------------------------------------------------------------- #
# Realtime Done enqueue (plan-0806-1 §4: single write Session)
# --------------------------------------------------------------------------- #
def enqueue_realtime_done(
    session: Session,
    oa_item_key: str,
    *,
    manifest_id: int | None = None,
    discovery_hash: str | None = None,
) -> tuple[bool, int]:
    """Enqueue an idempotent realtime Done download task within ``session``.

    Returns ``(created, task_id)``. Uses the caller's session so it never opens
    a nested write transaction (plan-0806-1 §4). The idempotency key embeds the
    discovery hash and ``archive-v2`` so an already-archived item (or one whose
    attachments are verified, or whose manifest archive status already
    succeeded) is not re-downloaded; an incomplete/ failed archive may still
    create a new version and retry.
    """
    key = f"realtime-done:{oa_item_key}:{discovery_hash or 'na'}:archive-v2"
    existing = session.scalar(select(PipelineTask.id).where(PipelineTask.idempotency_key == key))
    if existing is not None:
        return False, existing
    task = PipelineTask(
        queue_name="realtime_done",
        priority=QUEUE_PRIORITY["realtime_done"],
        logical_item_key=oa_item_key,
        stage="attachment_inventory",
        idempotency_key=key,
        payload_json=json.dumps({"manifest_id": manifest_id}, ensure_ascii=False),
    )
    session.add(task)
    session.flush()
    return True, task.id


# --------------------------------------------------------------------------- #
# Self-contained scheduled scans (plan-0806-1 §2.3: shared by CLI and worker)
# --------------------------------------------------------------------------- #
def _require_authenticated(browser: BrowserSession) -> None:
    if browser.login_with_saved_credentials(30) != LoginState.AUTHENTICATED:
        raise RuntimeError("OA authentication required")


def run_hourly_scan(engine, settings, *, headed: bool = False) -> dict:
    """Hourly working-hours scan: full Pending snapshot + Done known-boundary."""
    coordinator = ResourceCoordinator(engine)
    owner = f"schedule-hourly:{uuid4().hex}"
    lease_id = coordinator.acquire("oa_browser", owner, ttl_seconds=600, uses_local_gpu=False)
    if lease_id is None:
        raise RuntimeError("OA browser is busy")
    pending_summary: dict = {}
    done_summary: dict = {}
    status = "completed"
    try:
        with BrowserSession(settings, headed=headed) as browser:
            _require_authenticated(browser)
            assert browser.page
            with Session(engine) as session:
                run = record_scheduled_run(session, "scheduled_hourly")
                session.commit()
                run_id = run.id
            adapter = PendingAdapter(browser.page, f"{browser.base_url}{PENDING_LIST_PATH}")
            with Session(engine) as session:
                _, pending_summary = run_pending_scan(session, adapter, notification_mode="normal")
                session.commit()
            done_adapter = DoneAdapter(browser.page, f"{browser.base_url}{settings.browser.done_list_path}")
            known = _load_known_done_hashes(engine)
            boundary = scan_done_known_boundary(
                lambda page_number: done_adapter.discover_page(page_number, settings.collector.list_page_delay_seconds),
                is_known_unchanged=lambda item: _is_known_unchanged(known, item),
                known_boundary_count=20,
                max_pages=20,
            )
            enqueued = 0
            with Session(engine) as session:
                for item in boundary.new_items:
                    row = ensure_manifest_item(session, item.oa_item_key, title=item.title)
                    session.flush()
                    row.discovery_hash = _done_signature(item)
                    created, _ = enqueue_realtime_done(
                        session, item.oa_item_key, manifest_id=row.id, discovery_hash=_done_signature(item),
                    )
                    enqueued += int(created)
                done_summary = {
                    "source_total_pages": boundary.source_total_pages,
                    "pages_scanned": boundary.pages_scanned,
                    "new_items": len(boundary.new_items),
                    "known_items": boundary.reached_boundary or boundary.reached_max_pages,
                    "download_jobs_enqueued": enqueued,
                    "reached_boundary": boundary.reached_boundary,
                    "reached_max_pages": boundary.reached_max_pages,
                }
                status = "partial" if boundary.reached_max_pages else "completed"
                run = session.get(Run, run_id)
                close_scheduled_run(session, run, status, pending=pending_summary, done=done_summary)
                session.commit()
    finally:
        coordinator.release(lease_id, owner)
        engine.dispose()
    return {"status": status, "pending": pending_summary, "done": done_summary}


def run_nightly_scan(engine, settings, *, headed: bool = False) -> dict:
    """Nightly full sync: complete Done manifest, enqueue downloads, recover tasks."""
    coordinator = ResourceCoordinator(engine)
    owner = f"schedule-nightly:{uuid4().hex}"
    lease_id = coordinator.acquire("oa_browser", owner, ttl_seconds=1800, uses_local_gpu=False)
    if lease_id is None:
        raise RuntimeError("OA browser is busy")
    done_summary: dict = {}
    try:
        with BrowserSession(settings, headed=headed) as browser:
            _require_authenticated(browser)
            assert browser.page
            with Session(engine) as session:
                run = record_scheduled_run(session, "scheduled_nightly")
                session.commit()
                run_id = run.id
            done_adapter = DoneAdapter(browser.page, f"{browser.base_url}{settings.browser.done_list_path}")
            discovery = done_adapter.discover_all_pages()
            with Session(engine) as session:
                sync = synchronize_manifest(session, discovery)
                session.commit()
                pending = session.scalars(select(OAManifestItem).where(
                    OAManifestItem.processing_status.in_(("pending_download", "download_failed"))
                )).all()
                enqueued = 0
                for row in pending:
                    created, _ = enqueue_realtime_done(
                        session, row.oa_item_key, manifest_id=row.id, discovery_hash=row.discovery_hash,
                    )
                    enqueued += int(created)
                markdown_enqueued = enqueue_missing_markdown_tasks(engine, session=session)
                done_summary = {
                    "source_total": discovery.source_total_count,
                    "pages_scanned": discovery.pages_scanned,
                    "new_items": len(pending),
                    "download_jobs_enqueued": enqueued,
                    "markdown_tasks_enqueued": markdown_enqueued,
                    "manifest_sync_id": sync.id,
                }
                run = session.get(Run, run_id)
                close_scheduled_run(session, run, "completed", done=done_summary)
                session.commit()
    finally:
        coordinator.release(lease_id, owner)
        engine.dispose()
    return done_summary


def run_bootstrap_scan(engine, settings, *, headed: bool = False) -> dict:
    """First-deploy seeding: discover Pending without notifying, sync full Done."""
    coordinator = ResourceCoordinator(engine)
    owner = f"schedule-bootstrap:{uuid4().hex}"
    lease_id = coordinator.acquire("oa_browser", owner, ttl_seconds=1800, uses_local_gpu=False)
    if lease_id is None:
        raise RuntimeError("OA browser is busy")
    pending_summary: dict = {}
    done_summary: dict = {}
    try:
        with BrowserSession(settings, headed=headed) as browser:
            _require_authenticated(browser)
            assert browser.page
            with Session(engine) as session:
                run = record_scheduled_run(session, "scheduled_bootstrap")
                session.commit()
                run_id = run.id
            adapter = PendingAdapter(browser.page, f"{browser.base_url}{PENDING_LIST_PATH}")
            with Session(engine) as session:
                _, pending_summary = run_pending_scan(session, adapter, notification_mode="baseline")
                session.commit()
            done_adapter = DoneAdapter(browser.page, f"{browser.base_url}{settings.browser.done_list_path}")
            discovery = done_adapter.discover_all_pages()
            with Session(engine) as session:
                sync = synchronize_manifest(session, discovery)
                session.commit()
                done_summary = {"source_total": discovery.source_total_count, "manifest_sync_id": sync.id}
                run = session.get(Run, run_id)
                close_scheduled_run(session, run, "completed", pending=pending_summary, done=done_summary)
                session.commit()
    finally:
        coordinator.release(lease_id, owner)
        engine.dispose()
    return {"pending": pending_summary, "done": done_summary}
