from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.batches import BatchPlan, apply_business_exclusions, apply_discovery, batch_dict, cancel_batch, freeze_batch, pause_batch, plan_batch, recover_interrupted_items, resume_batch, retry_batch_item, reuse_verified_items
from oa_knowledge.collector.done import DiscoveredDoneItem
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import BatchItem, CollectionBatch, OAItem


def make_plan(limit: int = 20) -> BatchPlan:
    return BatchPlan(
        source_channel="done",
        window_start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        window_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
        planned_limit=limit,
    )


def test_plan_is_idempotent_and_deterministic(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        first, created = plan_batch(session, make_plan())
        session.commit()
        key = first.batch_key
    with Session(engine) as session:
        second, created_again = plan_batch(session, make_plan())
        assert created is True
        assert created_again is False
        assert second.batch_key == key


@pytest.mark.parametrize("limit", [0, 501])
def test_plan_limit_is_bounded(limit: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 500"):
        make_plan(limit).validate()


def test_freeze_is_idempotent_and_prevents_cancel(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        batch, _ = plan_batch(session, make_plan())
        session.commit()
        identifier = batch.batch_key
    with Session(engine) as session:
        frozen = freeze_batch(session, identifier)
        first_time = frozen.frozen_at
        session.commit()
    with Session(engine) as session:
        assert freeze_batch(session, identifier).frozen_at.replace(tzinfo=timezone.utc) == first_time
        with pytest.raises(ValueError, match="cannot be cancelled"):
            cancel_batch(session, identifier)


def test_unfrozen_plan_can_be_cancelled(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        batch, _ = plan_batch(session, make_plan())
        session.commit()
        payload = batch_dict(cancel_batch(session, batch.batch_key))
        assert payload["status"] == "cancelled"


def test_discovery_requires_freeze_and_filters_window(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    items = [
        DiscoveredDoneItem("-1", "inside", None, datetime(2026, 7, 10), "sender", None, "协同", 1),
        DiscoveredDoneItem("-2", "outside", None, datetime(2026, 8, 10), "sender", None, "协同", 2),
    ]
    with Session(engine) as session:
        batch, _ = plan_batch(session, make_plan())
        session.commit()
        with pytest.raises(ValueError, match="frozen"):
            apply_discovery(session, batch, items)
        freeze_batch(session, batch.batch_key)
        ready = apply_discovery(session, batch, items)
        session.commit()
        assert ready.discovered_count == 1
        assert ready.items[0].workitem_id_text == "-1"


def test_pause_resume_and_explicit_failed_item_retry(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        batch, _ = plan_batch(session, make_plan(limit=1))
        freeze_batch(session, batch.batch_key)
        apply_discovery(session, batch, [
            DiscoveredDoneItem("-1", "inside", None, datetime(2026, 7, 10), "sender", None, "协同", 1)
        ])
        batch.status = "running"
        batch_key = batch.batch_key
        session.flush()
        session.scalar(select(BatchItem).where(BatchItem.batch_id == batch.id)).archive_status = "download_failed"
        session.commit()
    with Session(engine) as session:
        assert str(pause_batch(session, batch_key).status) == "paused"
        assert str(resume_batch(session, batch_key).status) == "running"
        item = retry_batch_item(session, batch_key, 1)
        assert item.archive_status == "pending"
        session.commit()
    with Session(engine) as session:
        with pytest.raises(ValueError, match="not retryable"):
            retry_batch_item(session, batch_key, 1)


def test_interrupted_item_is_recovered_without_duplicate(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        batch, _ = plan_batch(session, make_plan(limit=1))
        session.flush()
        item = BatchItem(batch_id=batch.id, oa_item_key="done:stale", workitem_id_text="stale", ordinal=1, archive_status="archiving")
        session.add(item); session.commit(); batch_id = batch.id
    with Session(engine) as session:
        assert recover_interrupted_items(session, batch_id) == 1
        assert recover_interrupted_items(session, batch_id) == 0
        item = session.scalar(select(BatchItem).where(BatchItem.batch_id == batch_id))
        assert item.archive_status == "pending"
        assert item.retry_count == 1


def test_verified_item_is_reused_across_batches(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"; upgrade_database(db); engine = create_db_engine(db)
    with Session(engine) as session:
        oa_item = OAItem(oa_item_key="done:shared", source_channel="done", title="shared", pipeline_status="files_verified")
        first = CollectionBatch(batch_key="first", plan_hash="c" * 64, source_channel="done", planned_limit=1)
        second = CollectionBatch(batch_key="second", plan_hash="d" * 64, source_channel="done", planned_limit=1)
        session.add_all([oa_item, first, second]); session.flush()
        session.add_all([
            BatchItem(batch_id=first.id, oa_item_key="done:shared", workitem_id_text="shared", ordinal=1, oa_item_id=oa_item.id, archive_status="archived", archive_manifest_relpath="raw/shared/manifest.json"),
            BatchItem(batch_id=second.id, oa_item_key="done:shared", workitem_id_text="shared", ordinal=1),
        ]); session.commit(); second_id = second.id
    with Session(engine) as session:
        assert reuse_verified_items(session, second_id) == 1
        assert reuse_verified_items(session, second_id) == 0
        item = session.scalar(select(BatchItem).where(BatchItem.batch_id == second_id))
        assert item.archive_status == "archived" and item.oa_item_id is not None


def test_business_exclusion_is_auditable_and_only_applies_to_pending(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"; upgrade_database(db); engine = create_db_engine(db)
    with Session(engine) as session:
        batch = CollectionBatch(batch_key="policy", plan_hash="e" * 64, source_channel="done", planned_limit=2)
        session.add(batch); session.flush()
        session.add_all([
            BatchItem(batch_id=batch.id, oa_item_key="done:trip", workitem_id_text="trip", title="员工出差申请表", ordinal=1),
            BatchItem(batch_id=batch.id, oa_item_key="done:policy", workitem_id_text="policy", title="差旅管理制度", ordinal=2, archive_status="archived"),
        ]); session.commit(); batch_id = batch.id
    with Session(engine) as session:
        assert apply_business_exclusions(session, batch_id, ("出差申请", "报销单")) == 1
        trip = session.scalar(select(BatchItem).where(BatchItem.oa_item_key == "done:trip"))
        assert trip.archive_status == "confirmed_skip"
        assert trip.skip_reason == "excluded_title_pattern:出差申请"
        assert trip.policy_version == "business-exclusions-v1"
