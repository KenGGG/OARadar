"""Evidence-based Pending to Done lifecycle reconciliation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.collector.pending_detail import PendingDetailIdentifiers
from oa_knowledge.db.models import ItemOccurrence, LogicalItem, ReviewEntry, utcnow


@dataclass(frozen=True)
class MatchDecision:
    outcome: str
    logical_item_id: int | None
    pending_occurrence_id: int | None
    done_occurrence_id: int | None
    evidence: tuple[str, ...]


def reconcile_done_occurrence(
    session: Session,
    *,
    identifiers: PendingDetailIdentifiers,
    title: str,
    sender: str | None,
    completed_at: datetime | None,
) -> MatchDecision:
    """Link Done only when affair, summary, and process identities all agree."""
    required = (
        identifiers.affair_id_text,
        identifiers.summary_id_text,
        identifiers.process_id_text,
    )
    pending = None
    if all(required):
        pending = session.scalar(select(ItemOccurrence).where(
            ItemOccurrence.channel == "pending",
            ItemOccurrence.affair_id_text == identifiers.affair_id_text,
            ItemOccurrence.summary_id_text == identifiers.summary_id_text,
            ItemOccurrence.process_id_text == identifiers.process_id_text,
        ))
    if pending is None:
        container_key = f"done:{identifiers.affair_id_text or 'unknown'}"
        existing_review = session.scalar(select(ReviewEntry).where(
            ReviewEntry.kind == "pending_done_identity_review",
            ReviewEntry.container_key == container_key,
            ReviewEntry.status == "pending",
        ))
        if existing_review is None:
            session.add(ReviewEntry(
                kind="pending_done_identity_review",
                container_key=container_key,
                details_json=json.dumps({
                    "reason": "stable_identity_incomplete_or_unmatched",
                    "observed_fields": [name for name, value in asdict(identifiers).items() if value],
                }, ensure_ascii=False, sort_keys=True),
            ))
        session.flush()
        return MatchDecision("review", None, None, None, ())

    occurrence_key = f"done:{identifiers.affair_id_text}"
    done = session.scalar(select(ItemOccurrence).where(ItemOccurrence.occurrence_key == occurrence_key))
    if done is None:
        done = ItemOccurrence(
            logical_item_id=pending.logical_item_id,
            occurrence_key=occurrence_key,
            channel="done",
            first_seen_at=utcnow(),
        )
        session.add(done)
    done.title = title
    done.sender = sender
    done.received_at = completed_at
    done.processing_status = "done_confirmed"
    done.occurrence_status = "completed"
    done.affair_id_text = identifiers.affair_id_text
    done.summary_id_text = identifiers.summary_id_text
    done.process_id_text = identifiers.process_id_text
    done.activity_id_text = identifiers.activity_id_text
    done.case_id_text = identifiers.case_id_text
    done.workitem_id_text = identifiers.workitem_id_text
    done.form_record_id_text = identifiers.form_record_id_text
    done.object_id_text = identifiers.object_id_text
    done.template_id_text = identifiers.template_id_text
    done.identity_observed_at = utcnow()
    done.last_seen_at = utcnow()
    done.raw_fields_json = json.dumps({"identity": asdict(identifiers)}, ensure_ascii=False, sort_keys=True)
    pending.occurrence_status = "completed"
    logical = session.get(LogicalItem, pending.logical_item_id)
    if logical is not None:
        logical.lifecycle_status = "done_confirmed"
    session.flush()
    return MatchDecision(
        "exact", pending.logical_item_id, pending.id, done.id,
        ("affair_id", "summary_id", "process_id"),
    )
