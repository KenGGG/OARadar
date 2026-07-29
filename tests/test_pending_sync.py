from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from oa_knowledge.collector.pending import DiscoveredPendingItem
from oa_knowledge.collector.pending_detail import PendingDetailIdentifiers
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ItemOccurrence, ItemSnapshot, LogicalItem
from oa_knowledge.pending_sync import apply_pending_identifiers, record_pending_snapshot, sync_pending_discovery


def _item(affair_id: str, *, title: str = "Synthetic", node: str = "经办") -> DiscoveredPendingItem:
    return DiscoveredPendingItem(
        affair_id_text=affair_id, title=title, sender="Synthetic Sender", previous_approver=None,
        initiated_at=datetime(2026, 7, 24, 9), received_at=datetime(2026, 7, 24, 10),
        deadline_text="2026-07-25 18:00", reminder_count=0, processing_status="待处理",
        current_node=node, importance=None, ordinal=1,
    )


def test_pending_sync_is_idempotent_and_updates_changed_list_fields(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        first = sync_pending_discovery(session, [_item("affair-1")])
        second = sync_pending_discovery(session, [_item("affair-1", node="复核")])

        assert first.created == 1 and first.updated == 0
        assert second.created == 0 and second.updated == 1
        assert session.query(LogicalItem).count() == 1
        assert session.query(ItemOccurrence).count() == 1
        occurrence = session.query(ItemOccurrence).one()
        assert occurrence.current_node == "复核"
        assert occurrence.channel == "pending"
        assert occurrence.affair_id_text == "affair-1"


def test_same_title_with_different_affair_ids_stays_separate(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        result = sync_pending_discovery(session, [_item("affair-a"), _item("affair-b")])

        assert result.created == 2
        assert session.query(LogicalItem).count() == 2
        assert session.query(ItemOccurrence).count() == 2
        assert {row.lifecycle_status for row in session.query(LogicalItem)} == {"identity_pending"}


def test_authoritative_pending_snapshot_closes_missing_rows_and_reactivates_rediscovery(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        original = [_item(f"affair-{index}") for index in range(33)]
        sync_pending_discovery(session, original)

        refreshed = sync_pending_discovery(session, original[:5])

        assert refreshed.closed == 28
        assert session.query(ItemOccurrence).filter_by(channel="pending", occurrence_status="active").count() == 5
        assert session.query(ItemOccurrence).filter_by(channel="pending", occurrence_status="inactive").count() == 28

        rediscovered = sync_pending_discovery(session, [*original[:5], original[5]])

        assert rediscovered.reactivated == 1
        assert session.query(ItemOccurrence).filter_by(channel="pending", occurrence_status="active").count() == 6


def test_non_authoritative_pending_discovery_does_not_close_missing_rows(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        original = [_item(f"affair-{index}") for index in range(10)]
        sync_pending_discovery(session, original)

        result = sync_pending_discovery(session, original[:3], authoritative=False)

        assert result.closed == 0
        assert session.query(ItemOccurrence).filter_by(channel="pending", occurrence_status="active").count() == 10


def test_pending_detail_identifiers_update_occurrence_without_merging(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        sync_pending_discovery(session, [_item("affair-1")])
        identifiers = PendingDetailIdentifiers(
            affair_id_text="affair-1", summary_id_text="summary-1", process_id_text="process-1",
            activity_id_text="activity-1", case_id_text="case-1", workitem_id_text="workitem-1",
            form_record_id_text=None, object_id_text=None, template_id_text=None,
        )

        occurrence = apply_pending_identifiers(session, "pending:affair-1", identifiers)

        assert occurrence.process_id_text == "process-1"
        assert occurrence.summary_id_text == "summary-1"
        assert occurrence.activity_id_text == "activity-1"
        assert occurrence.case_id_text == "case-1"
        assert occurrence.workitem_id_text == "workitem-1"
        assert session.query(LogicalItem).one().lifecycle_status == "identity_pending"


def test_pending_snapshot_reuses_unchanged_content_and_versions_changes(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        sync_pending_discovery(session, [_item("affair-1")])
        identifiers = PendingDetailIdentifiers(
            affair_id_text="affair-1", summary_id_text="summary-1", process_id_text="process-1",
            activity_id_text="activity-1", case_id_text="case-1", workitem_id_text="workitem-1",
            form_record_id_text=None, object_id_text=None, template_id_text=None,
        )
        apply_pending_identifiers(session, "pending:affair-1", identifiers)

        initial = record_pending_snapshot(session, "pending:affair-1", {"body_hash": "a" * 64})
        repeated = record_pending_snapshot(session, "pending:affair-1", {"body_hash": "a" * 64})
        updated = record_pending_snapshot(session, "pending:affair-1", {"body_hash": "b" * 64})
        repeated_update = record_pending_snapshot(session, "pending:affair-1", {"body_hash": "b" * 64})

        assert initial.id == repeated.id
        assert initial.snapshot_kind == "pending_initial"
        assert updated.id == repeated_update.id
        assert updated.snapshot_kind == "pending_updated"
        assert updated.id != initial.id
        assert session.query(ItemSnapshot).count() == 2
        assert initial.payload_json != updated.payload_json


def test_pending_snapshot_embeds_stable_occurrence_source_context(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        sync_pending_discovery(session, [_item("affair-1")])
        snapshot = record_pending_snapshot(session, "pending:affair-1", {"attachment_count": 2})

        import json
        payload = json.loads(snapshot.payload_json)
        assert payload["source"]["occurrence_key"] == "pending:affair-1"
        assert payload["source"]["affair_id"] == "affair-1"
        assert payload["detail"] == {"attachment_count": 2}
