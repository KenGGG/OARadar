from datetime import datetime

from sqlalchemy.orm import Session

from oa_knowledge.collector.pending import DiscoveredPendingItem
from oa_knowledge.collector.pending_detail import PendingDetailIdentifiers
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ItemOccurrence, LogicalItem, ReviewEntry
from oa_knowledge.pending_sync import apply_pending_identifiers, sync_pending_discovery
from oa_knowledge.reconcile import reconcile_done_occurrence


def _pending(session: Session, affair: str, title: str = "Synthetic") -> ItemOccurrence:
    sync_pending_discovery(session, [DiscoveredPendingItem(
        affair_id_text=affair, title=title, sender="Synthetic Sender", previous_approver=None,
        initiated_at=None, received_at=None, deadline_text=None, reminder_count=0,
        processing_status="待处理", current_node="经办", importance=None, ordinal=1,
    )])
    return apply_pending_identifiers(session, f"pending:{affair}", PendingDetailIdentifiers(
        affair_id_text=affair, summary_id_text="summary-1", process_id_text="process-1",
        activity_id_text="activity-pending", case_id_text="case-1", workitem_id_text="work-pending",
        form_record_id_text=None, object_id_text=None, template_id_text=None,
    ))


def test_reconcile_done_uses_stable_identity_not_title_or_workitem(tmp_path) -> None:
    db = tmp_path / "oa.db"; upgrade_database(db); engine = create_db_engine(db)
    with Session(engine) as session:
        pending = _pending(session, "affair-1")
        decision = reconcile_done_occurrence(
            session,
            identifiers=PendingDetailIdentifiers(
                affair_id_text="affair-1", summary_id_text="summary-1", process_id_text="process-1",
                activity_id_text="activity-done", case_id_text="case-1", workitem_id_text="work-done",
                form_record_id_text=None, object_id_text=None, template_id_text=None,
            ),
            title="Changed title", sender="Synthetic Sender", completed_at=datetime(2026, 7, 27, 9, 17),
        )
        session.commit()
        done = session.query(ItemOccurrence).filter_by(channel="done").one()
        logical = session.get(LogicalItem, pending.logical_item_id)
        assert decision.outcome == "exact"
        assert done.logical_item_id == pending.logical_item_id
        assert done.workitem_id_text == "work-done"
        assert pending.occurrence_status == "completed"
        assert logical is not None and logical.lifecycle_status == "done_confirmed"


def test_reconcile_done_does_not_merge_on_title_or_partial_identity(tmp_path) -> None:
    db = tmp_path / "oa.db"; upgrade_database(db); engine = create_db_engine(db)
    with Session(engine) as session:
        _pending(session, "affair-1", title="Duplicate title")
        decision = reconcile_done_occurrence(
            session,
            identifiers=PendingDetailIdentifiers(
                affair_id_text="affair-other", summary_id_text="summary-1", process_id_text=None,
                activity_id_text=None, case_id_text=None, workitem_id_text="work-other",
                form_record_id_text=None, object_id_text=None, template_id_text=None,
            ),
            title="Duplicate title", sender=None, completed_at=None,
        )
        assert decision.outcome == "review"
        assert session.query(ItemOccurrence).filter_by(channel="done").count() == 0

        reconcile_done_occurrence(
            session,
            identifiers=PendingDetailIdentifiers(
                affair_id_text="affair-other", summary_id_text="summary-1", process_id_text=None,
                activity_id_text=None, case_id_text=None, workitem_id_text="work-other",
                form_record_id_text=None, object_id_text=None, template_id_text=None,
            ),
            title="Duplicate title", sender=None, completed_at=None,
        )
        assert session.query(ReviewEntry).filter_by(kind="pending_done_identity_review").count() == 1
