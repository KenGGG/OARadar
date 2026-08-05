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

from oa_knowledge.db.models import OAManifestItem, PipelineTask, Run
from oa_knowledge.pending_sync import PendingSyncResult, sync_pending_discovery
from oa_knowledge.production_pipeline import ProductionQueue


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
# Realtime Done enqueue (plan-0805-02 §2.4)
# --------------------------------------------------------------------------- #
def enqueue_realtime_done(engine, oa_item_key: str, *, manifest_id: int | None = None) -> tuple[bool, int]:
    """Enqueue an idempotent realtime Done download task.

    Returns ``(created, task_id)``. The idempotency key
    ``realtime-done:<oa_item_key>:archive-v1`` ensures a newly discovered item
    is downloaded exactly once and survives the next hourly run.
    """
    key = f"realtime-done:{oa_item_key}:archive-v1"
    with Session(engine) as session:
        existing = session.scalar(select(PipelineTask.id).where(PipelineTask.idempotency_key == key))
        if existing is not None:
            return False, existing
    queue = ProductionQueue(engine)
    task_id = queue.enqueue(
        "realtime_done", oa_item_key, "attachment_inventory", key,
        payload={"manifest_id": manifest_id},
    )
    return True, task_id


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
