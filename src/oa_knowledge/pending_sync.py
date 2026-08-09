"""Idempotent persistence for bounded pending-list discovery."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.collector.pending import DiscoveredPendingItem
from oa_knowledge.collector.pending_detail import PendingDetailIdentifiers
from oa_knowledge.db.models import ItemOccurrence, ItemSnapshot, LogicalItem, PipelineTask, utcnow
from oa_knowledge.lifecycle import record_snapshot


@dataclass(frozen=True)
class PendingSyncResult:
    created: int
    updated: int
    unchanged: int
    closed: int = 0
    reactivated: int = 0


def _serialized(item: DiscoveredPendingItem) -> tuple[str, str]:
    payload = asdict(item)
    for key in ("initiated_at", "received_at"):
        value = payload[key]
        payload[key] = value.isoformat() if isinstance(value, datetime) else None
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sync_pending_discovery(
    session: Session,
    items: list[DiscoveredPendingItem],
    *,
    authoritative: bool = True,
    notification_mode: str = "normal",
) -> PendingSyncResult:
    """Persist a Pending-list discovery snapshot idempotently.

    ``notification_mode`` controls whether discovered items advance into the
    Feishu notify stage (plan-0805-02 §1.2):

    * ``normal``   — new and changed items are notified once.
    * ``baseline`` — items are archived and summarized but never notified; used
      for first-deploy bootstrapping so the existing backlog is not pushed.
    * ``disabled`` — only the occurrence state is synced; no pipeline tasks are
      enqueued at all.
    """
    created = updated = unchanged = closed = reactivated = 0
    seen_keys = {item.occurrence_key for item in items}
    for item in items:
        raw, discovery_hash = _serialized(item)
        occurrence = session.scalar(
            select(ItemOccurrence).where(ItemOccurrence.occurrence_key == item.occurrence_key)
        )
        if occurrence is None:
            key_hash = hashlib.sha256(item.affair_id_text.encode("utf-8")).hexdigest()[:24]
            logical = LogicalItem(
                logical_key=f"pending-provisional:{key_hash}",
                title=item.title,
                lifecycle_status="identity_pending",
            )
            session.add(logical)
            session.flush()
            occurrence = ItemOccurrence(
                logical_item_id=logical.id,
                occurrence_key=item.occurrence_key,
                channel="pending",
                affair_id_text=item.affair_id_text,
                first_seen_at=utcnow(),
            )
            session.add(occurrence)
            created += 1
            enqueue_pipeline = True
        else:
            if occurrence.occurrence_status != "active":
                occurrence.occurrence_status = "active"
                reactivated += 1
            changed = occurrence.discovery_hash != discovery_hash
            if changed:
                updated += 1
                enqueue_pipeline = True
            else:
                unchanged += 1
                enqueue_pipeline = False

        # Always refresh display columns from the latest OA snapshot so a
        # re-discovery restores titles the post-send cleanup erased, keeping the
        # local 待办 list in sync with OA without re-running the pipeline for
        # unchanged items.
        occurrence.title = item.title
        occurrence.sender = item.sender
        occurrence.previous_approver = item.previous_approver
        occurrence.initiated_at = item.initiated_at
        occurrence.received_at = item.received_at
        occurrence.deadline_text = item.deadline_text
        occurrence.reminder_count = item.reminder_count
        occurrence.processing_status = item.processing_status
        occurrence.current_node = item.current_node
        occurrence.importance = item.importance
        occurrence.raw_fields_json = raw
        occurrence.discovery_hash = discovery_hash
        occurrence.last_seen_at = utcnow()
        session.flush()
        if notification_mode == "disabled":
            # Sync occurrence state only; do not enqueue capture/summary/notify.
            continue
        if enqueue_pipeline:
            notify = notification_mode == "normal"
        task_key = f"pending:{occurrence.occurrence_key}:{discovery_hash}:detail-v1"
        if session.scalar(select(PipelineTask.id).where(PipelineTask.idempotency_key == task_key)) is None:
            session.add(PipelineTask(
                queue_name="realtime_pending", priority=0, logical_item_key=str(occurrence.logical_item_id),
                logical_item_id=occurrence.logical_item_id, stage="detail_sync", idempotency_key=task_key,
                max_attempts=5,
                payload_json=json.dumps({
                    "occurrence_id": occurrence.id,
                    "baseline": notification_mode == "baseline",
                    "notify": notify,
                }),
            ))
    if authoritative:
        active_rows = session.scalars(select(ItemOccurrence).where(
            ItemOccurrence.channel == "pending",
            ItemOccurrence.occurrence_status == "active",
        )).all()
        for occurrence in active_rows:
            if occurrence.occurrence_key not in seen_keys:
                occurrence.occurrence_status = "inactive"
                closed += 1
    session.flush()
    return PendingSyncResult(
        created=created,
        updated=updated,
        unchanged=unchanged,
        closed=closed,
        reactivated=reactivated,
    )


def apply_pending_identifiers(
    session: Session,
    occurrence_key: str,
    identifiers: PendingDetailIdentifiers,
) -> ItemOccurrence:
    occurrence = session.scalar(
        select(ItemOccurrence).where(ItemOccurrence.occurrence_key == occurrence_key)
    )
    if occurrence is None:
        raise LookupError(f"pending occurrence not found: {occurrence_key}")
    if identifiers.affair_id_text and identifiers.affair_id_text != occurrence.affair_id_text:
        raise ValueError("detail affairId does not match pending occurrence")
    occurrence.summary_id_text = identifiers.summary_id_text
    occurrence.process_id_text = identifiers.process_id_text
    occurrence.activity_id_text = identifiers.activity_id_text
    occurrence.case_id_text = identifiers.case_id_text
    occurrence.workitem_id_text = identifiers.workitem_id_text
    occurrence.form_record_id_text = identifiers.form_record_id_text
    occurrence.object_id_text = identifiers.object_id_text
    occurrence.template_id_text = identifiers.template_id_text
    occurrence.identity_observed_at = utcnow()
    session.flush()
    return occurrence


def record_pending_snapshot(
    session: Session,
    occurrence_key: str,
    detail_payload: dict,
) -> ItemSnapshot:
    """Record an immutable pending snapshot while preserving its OA source context."""
    occurrence = session.scalar(
        select(ItemOccurrence).where(
            ItemOccurrence.occurrence_key == occurrence_key,
            ItemOccurrence.channel == "pending",
        )
    )
    if occurrence is None:
        raise LookupError(f"pending occurrence not found: {occurrence_key}")

    payload = {
        "source": {
            "occurrence_key": occurrence.occurrence_key,
            "affair_id": occurrence.affair_id_text,
            "summary_id": occurrence.summary_id_text,
            "process_id": occurrence.process_id_text,
            "activity_id": occurrence.activity_id_text,
            "case_id": occurrence.case_id_text,
            "workitem_id": occurrence.workitem_id_text,
            "form_record_id": occurrence.form_record_id_text,
            "object_id": occurrence.object_id_text,
            "template_id": occurrence.template_id_text,
        },
        "detail": detail_payload,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    existing = session.scalar(
        select(ItemSnapshot).where(
            ItemSnapshot.logical_item_id == occurrence.logical_item_id,
            ItemSnapshot.snapshot_kind.in_(("pending_initial", "pending_updated")),
            ItemSnapshot.content_hash == content_hash,
        )
    )
    if existing is not None:
        return existing
    has_snapshot = session.scalar(
        select(ItemSnapshot.id).where(
            ItemSnapshot.logical_item_id == occurrence.logical_item_id,
            ItemSnapshot.snapshot_kind.in_(("pending_initial", "pending_updated")),
        ).limit(1)
    )
    return record_snapshot(
        session,
        occurrence.logical_item_id,
        occurrence.id,
        "pending_updated" if has_snapshot is not None else "pending_initial",
        payload,
    )


def resync_pending_item_from_oa(
    settings,
    engine,
    occurrence_id: int,
    *,
    discovery=None,
) -> bool:
    """Re-fetch one cleaned Pending item's display columns from OA.

    Used by the web console's "与 OA 同步" action (plan-0807-1 §sync). Opens the
    OA pending list, finds the row whose ``affair_id`` matches the occurrence,
    and refreshes only the display columns (title/sender/current_node/deadline)
    via ``sync_pending_discovery(..., notification_mode="disabled")`` — this
    restores the title without re-running capture/summary/notify or persisting
    any business body.

    Returns ``True`` when the item was found in OA (and its columns refreshed),
    ``False`` when it is no longer present (in which case ``oa_gone_at`` is set so
    the UI can report it as unrecoverable). ``discovery`` may be injected for
    tests to avoid a real browser session.
    """
    from oa_knowledge.collector.pending import PendingAdapter

    with Session(engine) as session:
        occurrence = session.get(ItemOccurrence, occurrence_id)
        if occurrence is None or occurrence.channel != "pending":
            return False
        affair_id = occurrence.affair_id_text
        if not affair_id:
            return False

        if discovery is None:
            from oa_knowledge.collector import BrowserSession, LoginState
            with BrowserSession(settings, headed=False) as browser:
                if browser.login_with_saved_credentials(30) != LoginState.AUTHENTICATED:
                    raise RuntimeError("OA login required for pending re-sync")
                assert browser.page
                adapter = PendingAdapter(browser.page, f"{browser.base_url}{PendingAdapter.list_path}")
                discovery = adapter.discover_all_pages()

        matched = next(
            (it for it in discovery.items if it.affair_id_text == affair_id), None
        )
        if matched is None:
            occurrence.oa_gone_at = utcnow()
            session.commit()
            return False

        occurrence.oa_gone_at = None
        sync_pending_discovery(session, [matched], authoritative=False, notification_mode="disabled")
        session.commit()
        return True

