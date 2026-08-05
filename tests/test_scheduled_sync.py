"""Tests for scheduled sync orchestration (plan-0805-02 §2)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from oa_knowledge.collector.pending import DiscoveredPendingItem
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import OAManifestItem, PipelineTask, Run
from oa_knowledge.scheduled_sync import (
    close_scheduled_run,
    enqueue_realtime_done,
    ensure_manifest_item,
    record_scheduled_run,
    run_pending_scan,
    scan_done_known_boundary,
)


class _FakeDoneItem:
    def __init__(self, key: str) -> None:
        self.oa_item_key = key


class _FakePage:
    def __init__(self, items, is_last_page, source_total_pages=None):
        self.items = items
        self.is_last_page = is_last_page
        self.source_total_pages = source_total_pages


def _pending_item(affair_id: str) -> DiscoveredPendingItem:
    return DiscoveredPendingItem(
        affair_id_text=affair_id, title="Synthetic", sender="S", previous_approver=None,
        initiated_at=datetime(2026, 7, 24, 9), received_at=datetime(2026, 7, 24, 10),
        deadline_text="2026-07-25", reminder_count=0, processing_status="待处理",
        current_node="经办", importance=None, ordinal=1,
    )


# --------------------------------------------------------------------------- #
# Run ledger
# --------------------------------------------------------------------------- #
def test_record_and_close_scheduled_run(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        run = record_scheduled_run(session, "scheduled_hourly")
        session.commit()
        run_id = run.id
        assert run.status == "running"
        close_scheduled_run(session, run, "completed", pending={"created": 1}, done={"new_items": 2})
        session.commit()
    with Session(engine) as session:
        row = session.get(Run, run_id)
        assert row.status == "completed"
        summary = __import__("json").loads(row.summary_json)
        assert summary["pending"] == {"created": 1}
        assert summary["status"] == "completed"
        assert summary["finished_at"] is not None


# --------------------------------------------------------------------------- #
# Done known-boundary scan
# --------------------------------------------------------------------------- #
def test_known_boundary_stops_at_stable_boundary() -> None:
    # Page 1 has 2 new items; pages 2..3 each have 20 known (unchanged) -> boundary.
    pages = [
        _FakePage([_FakeDoneItem(f"new-{i}") for i in range(2)], is_last_page=False, source_total_pages=10),
    ]
    for _ in range(2):
        pages.append(_FakePage([_FakeDoneItem(f"known-{i}") for i in range(20)], is_last_page=False, source_total_pages=10))
    known = {"known-0"}  # only need the predicate to say "known" for the boundary items

    def is_known(item):
        return item.oa_item_key.startswith("known-")

    result = scan_done_known_boundary(lambda n: pages[n - 1], is_known_unchanged=is_known, known_boundary_count=20, max_pages=20)
    assert result.reached_boundary is True
    assert result.reached_max_pages is False
    assert len(result.new_items) == 2
    # Boundary is hit on page 2 (20 consecutive known items), so page 3 is never read.
    assert result.pages_scanned == 2


def test_known_boundary_stops_at_last_page() -> None:
    pages = [
        _FakePage([_FakeDoneItem("new-0")], is_last_page=False, source_total_pages=2),
        _FakePage([_FakeDoneItem("new-1")], is_last_page=True, source_total_pages=2),
    ]
    result = scan_done_known_boundary(lambda n: pages[n - 1], is_known_unchanged=lambda _: False, max_pages=20)
    assert result.reached_boundary is False
    assert result.pages_scanned == 2
    assert len(result.new_items) == 2


def test_known_boundary_hits_max_pages_is_partial() -> None:
    # Every item is "new" (never known) -> never reaches a boundary, hits ceiling.
    pages = [_FakePage([_FakeDoneItem(f"new-{p}-{i}") for i in range(5)], is_last_page=False, source_total_pages=100)
             for p in range(20)]
    result = scan_done_known_boundary(lambda n: pages[n - 1], is_known_unchanged=lambda _: False, known_boundary_count=20, max_pages=20)
    assert result.reached_max_pages is True
    assert result.reached_boundary is False
    assert result.pages_scanned == 20


# --------------------------------------------------------------------------- #
# Pending full snapshot
# --------------------------------------------------------------------------- #
def test_run_pending_scan_uses_full_snapshot(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)

    class _FakeDiscovery:
        items = (_pending_item("affair-1"), _pending_item("affair-2"))
        pages_scanned = 2
        source_total_pages = 2
        source_total_count = 2

    class _FakeAdapter:
        def discover_all_pages(self, page_delay_seconds: float = 0):
            return _FakeDiscovery()

    with Session(engine) as session:
        result, summary = run_pending_scan(session, _FakeAdapter(), notification_mode="normal")
        session.commit()
        assert result.created == 2
        assert summary["tasks_enqueued"] == 2
        # New items are notified (notify=True) in normal mode.
        assert session.query(PipelineTask).filter_by(stage="detail_sync").count() == 2
        for task in session.query(PipelineTask).all():
            assert __import__("json").loads(task.payload_json)["notify"] is True


# --------------------------------------------------------------------------- #
# Realtime Done enqueue idempotency (§2.4)
# --------------------------------------------------------------------------- #
def test_enqueue_realtime_done_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    created_first, task_id = enqueue_realtime_done(engine, "done:123")
    assert created_first is True
    with Session(engine) as session:
        task = session.get(PipelineTask, task_id)
        assert task.queue_name == "realtime_done"
        assert task.stage == "attachment_inventory"
        assert task.idempotency_key == "realtime-done:done:123:archive-v1"
    created_second, same_id = enqueue_realtime_done(engine, "done:123")
    assert created_second is False
    assert same_id == task_id


def test_ensure_manifest_item_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        first = ensure_manifest_item(session, "done:abc", title="A")
        session.flush()
        second = ensure_manifest_item(session, "done:abc", title="B")
        session.flush()
        assert first.id == second.id
        assert session.query(OAManifestItem).filter_by(oa_item_key="done:abc").count() == 1
