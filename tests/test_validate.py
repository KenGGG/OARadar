from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oa_knowledge.batches import (
    BatchPlan,
    apply_discovery,
    batch_dict,
    freeze_batch,
    plan_batch,
    validate_batch,
)
from oa_knowledge.collector.done import DiscoveredDoneItem
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, BatchItem, CollectionBatch, OAItem
from oa_knowledge.ops.capacity import scale_capacity_report


def make_plan(**kw) -> BatchPlan:
    kwargs = dict(
        source_channel="done",
        window_start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        window_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
        planned_limit=20,
    )
    kwargs.update(kw)
    return BatchPlan(**kwargs)


# ---------------------------------------------------------------------------
# validate_batch – reconciliation
# ---------------------------------------------------------------------------


def test_validate_reconciles_when_counts_match(tmp_path: Path) -> None:
    db = tmp_path / "state" / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        batch, _ = plan_batch(session, make_plan())
        freeze_batch(session, batch.batch_key)
        apply_discovery(session, batch, [
            DiscoveredDoneItem("-1", "a", None, datetime(2026, 7, 10), "s", None, "协同", 1),
            DiscoveredDoneItem("-2", "b", None, datetime(2026, 7, 11), "s", None, "协同", 2),
        ])
        session.commit()
        batch_key = batch.batch_key

    with Session(engine) as session:
        report = validate_batch(session, batch_key)
        assert report.discovered == 2
        assert report.archived == 0
        assert report.unresolved == 2
        assert not report.reconciled


def test_validate_passes_when_all_archived(tmp_path: Path) -> None:
    db = tmp_path / "state" / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        batch, _ = plan_batch(session, make_plan())
        freeze_batch(session, batch.batch_key)
        apply_discovery(session, batch, [
            DiscoveredDoneItem("-1", "a", None, datetime(2026, 7, 10), "s", None, "协同", 1),
        ])
        item = session.scalar(select(BatchItem).where(BatchItem.batch_id == batch.id))
        item.archive_status = "archived"
        item.archived_at = datetime.now(timezone.utc)
        batch.archived_count = 1
        session.commit()
        batch_key = batch.batch_key

    with Session(engine) as session:
        report = validate_batch(session, batch_key)
        assert report.reconciled
        assert report.archived == 1
        assert report.unresolved == 0


def test_validate_treats_review_required_as_reconciled_terminal_result(tmp_path: Path) -> None:
    db = tmp_path / "state" / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        batch, _ = plan_batch(session, make_plan())
        freeze_batch(session, batch.batch_key)
        apply_discovery(session, batch, [
            DiscoveredDoneItem("-1", "large", None, datetime(2026, 7, 10), "s", None, "协同", 1),
        ])
        item = session.scalar(select(BatchItem).where(BatchItem.batch_id == batch.id))
        item.archive_status = "review_required"
        batch.failed_count = 1
        session.commit()
        report = validate_batch(session, batch.batch_key)
        assert report.reconciled
        assert report.reviewed == 1
        assert report.failed == 0
        assert report.unresolved == 0


def test_validate_checks_source_query_count(tmp_path: Path) -> None:
    db = tmp_path / "state" / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        batch, _ = plan_batch(session, make_plan())
        freeze_batch(session, batch.batch_key)
        apply_discovery(session, batch, [
            DiscoveredDoneItem("-1", "a", None, datetime(2026, 7, 10), "s", None, "协同", 1),
        ])
        batch.query_count = 999  # mismatch with discovered_count == 1
        session.commit()
        batch_key = batch.batch_key

    with Session(engine) as session:
        report = validate_batch(session, batch_key)
        assert report.query_count == 999
        assert not report.source_match


def test_validate_accepts_source_rows_filtered_at_manifest_boundary(tmp_path: Path) -> None:
    db = tmp_path / "state" / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        batch, _ = plan_batch(session, make_plan())
        freeze_batch(session, batch.batch_key)
        apply_discovery(session, batch, [
            DiscoveredDoneItem("-1", "a", None, datetime(2026, 7, 10), "s", None, "协同", 1),
        ])
        batch.query_count = 2
        batch.scanned_row_count = 2
        batch.source_total_count = 2
        session.commit()
        report = validate_batch(session, batch.batch_key)
        assert report.source_match


# ---------------------------------------------------------------------------
# scale_capacity_report
# ---------------------------------------------------------------------------


def test_scale_report_allows_when_sufficient_disk(tmp_path: Path) -> None:
    db = tmp_path / "state" / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        item = OAItem(oa_item_key="one", source_channel="done", title="one")
        session.add(item)
        session.flush()
        session.add(ArchivedFile(
            oa_item_id=item.id, original_name="one.pdf", attachment_key="one",
            file_role="direct_attachment", source_container_key="one", depth=1,
            size_bytes=5000, download_status="verified",
        ))
        session.commit()

    report = scale_capacity_report(db, tmp_path, target_items=500, safety_factor=1.5)
    assert report.current_items == 1
    assert report.new_items == 499
    assert report.allowed


def test_scale_report_warns_when_already_at_target(tmp_path: Path) -> None:
    db = tmp_path / "state" / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        for i in range(5):
            item = OAItem(oa_item_key=f"x{i}", source_channel="done", title=f"x{i}")
            session.add(item)
        session.commit()

    report = scale_capacity_report(db, tmp_path, target_items=3, safety_factor=1.5)
    assert any("already_at_target" in w for w in report.warnings)
