"""Pending (待办) notification data cleanup (plan-0807-1 §6).

Pending items are short-lived notification records, not permanent knowledge
assets. Once Feishu confirms a successful delivery, the business payload — the
OA body, opinion text, page snapshots, temporary attachments, summaries and
delivery copies — is erased, leaving only the minimal ledger required to prevent
duplicate notifications on the next scan of the same stable item version.

The original *done* (已办) archive is never touched by this module: pending
business files live under their own ``oa_items`` rows (``source_channel ==
'pending'``), so deleting those rows can never remove a permanent done original.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.models import (
    ArchivedFile, ContentObject, ItemOccurrence, ItemSnapshot, LogicalItem,
    NotificationDelivery, OAItem, ParseArtifact, ParseJob, SourceAttachment,
    SummaryEvidence, SummaryJob, SummaryVersion,
)
from oa_knowledge.storage_paths import resolve_data_path

# Delivery statuses that mean "do not clean yet" — the payload must stay so the
# worker can retry or the operator can inspect the outcome.
BLOCKING_DELIVERY_STATUSES = {
    "queued", "retry_wait", "unknown", "failed", "rejected",
    "misconfigured", "server_failed", "connect_failed", "unknown_outcome",
}

CLEANED = "cleaned"
CLEANING = "cleaning"
PENDING_CLEANUP = "pending_cleanup"
NOT_ELIGIBLE = "not_eligible"
CLEANUP_FAILED = "cleanup_failed"


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def delivery_for_occurrence(session: Session, occurrence: ItemOccurrence) -> NotificationDelivery | None:
    if occurrence.logical_item_id is None:
        return None
    return session.scalar(select(NotificationDelivery).where(
        NotificationDelivery.logical_item_id == occurrence.logical_item_id,
        NotificationDelivery.channel == "feishu",
        NotificationDelivery.notification_type == "pending_summary",
    ).order_by(NotificationDelivery.id.desc()).limit(1))


def cleanup_eligibility(
    occurrence: ItemOccurrence,
    delivery: NotificationDelivery | None,
    settings: Settings,
    now: datetime,
    *,
    force: bool = False,
) -> tuple[bool, str]:
    """Return ``(eligible, reason)`` for cleaning this occurrence's business data.

    Every cleanup path requires a confirmed successful Feishu delivery. ``force``
    may bypass the automatic-policy switch or retry a previous cleanup failure,
    but can never bypass delivery confirmation.
    """
    status = occurrence.cleanup_status or NOT_ELIGIBLE
    if status in {CLEANED, CLEANING}:
        return False, "already_cleaned" if status == CLEANED else "already_cleaning"
    if status == CLEANUP_FAILED and not force:
        return False, "previous_cleanup_failed"

    if delivery is None:
        return False, "no_delivery_record"
    if delivery.status != "sent":
        return False, f"delivery_not_sent:{delivery.status}"
    if force:
        if not settings.pending_cleanup.allow_force_cleanup:
            return False, "force_cleanup_disabled"
        return True, "delivery_sent_manual"
    if not settings.pending_cleanup.auto_cleanup_after_success:
        return False, "auto_cleanup_disabled"
    return True, "delivery_sent"


def _delete_archived_file(session: Session, settings: Settings, file: ArchivedFile) -> None:
    """Remove one ArchivedFile row, its physical file, and orphaned content/parse rows.

    Physical deletion is guarded so it can only touch a pending file under the
    data root. A content object is dropped only when no other ArchivedFile still
    references it.
    """
    content_object_id = file.content_object_id
    local_relpath = file.local_relpath
    if local_relpath:
        target = resolve_data_path(
            settings.data_root,
            local_relpath,
            allowed_prefixes=("raw/pending", "archive/raw/oa/pending"),
        )
        if target.exists():
            if not target.is_file():
                raise OSError("pending archived path is not a regular file")
            target.unlink()

    # Do not mutate database relationships until physical deletion succeeds.
    for job in file.parse_jobs:
        session.delete(job)
    session.flush()
    session.delete(file)
    if content_object_id is not None:
        remaining = session.scalar(
            select(ArchivedFile).where(ArchivedFile.content_object_id == content_object_id).limit(1)
        )
        if remaining is None:
            content = session.get(ContentObject, content_object_id)
            if content is not None:
                session.delete(content)


def _null_business_columns(occurrence: ItemOccurrence) -> None:
    """Erase business payload columns while keeping the de-duplication ledger."""
    occurrence.title = None
    occurrence.sender = None
    occurrence.current_node = None
    occurrence.previous_approver = None
    occurrence.department = None
    occurrence.deadline_text = None
    occurrence.raw_fields_json = "{}"
    # Keep: identity_*_text, discovery_hash, first_seen_at, last_seen_at,
    # detail_url, occurrence_key, logical_item_id, channel.


def perform_cleanup(
    session: Session,
    occurrence: ItemOccurrence,
    settings: Settings,
    now: datetime,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Delete the business payload for ``occurrence`` and retain the minimal ledger.

    Returns a summary dict. Raises nothing for ordinary data problems — failures
    are recorded on the occurrence as ``cleanup_failed`` so the operator can retry.
    """
    delivery = delivery_for_occurrence(session, occurrence)
    eligible, reason = cleanup_eligibility(occurrence, delivery, settings, now, force=force)
    if not eligible:
        raise ValueError(f"cleanup not eligible: {reason}")

    occurrence.cleanup_status = CLEANING
    occurrence.cleanup_requested_at = now
    occurrence.cleanup_attempts = (occurrence.cleanup_attempts or 0) + 1
    session.flush()

    cfg = settings.pending_cleanup
    logical_item_id = occurrence.logical_item_id
    try:
        if not cfg.keep_page_snapshot and logical_item_id is not None:
            snapshots = session.scalars(
                select(ItemSnapshot).where(ItemSnapshot.logical_item_id == logical_item_id)
            ).all()
            snapshot_ids = [s.id for s in snapshots]
            if snapshot_ids:
                sources = session.scalars(
                    select(SourceAttachment).where(SourceAttachment.snapshot_id.in_(snapshot_ids))
                ).all()
                for source in sources:
                    if source.source_file_id is not None:
                        af = session.get(ArchivedFile, source.source_file_id)
                        if af is not None:
                            _delete_archived_file(session, settings, af)
                    # ItemSnapshot owns SourceAttachment via ON DELETE CASCADE.
                    # Deleting both explicitly made SQLite delete the child
                    # first and SQLAlchemy later warn that its duplicate DELETE
                    # matched zero rows.
                for snap in snapshots:
                    session.delete(snap)

        if not cfg.keep_temp_attachments and logical_item_id is not None:
            pending_items = session.scalars(select(OAItem).where(
                OAItem.logical_item_id == logical_item_id,
                OAItem.source_channel == "pending",
            )).all()
            for item in pending_items:
                for file in list(item.files):
                    _delete_archived_file(session, settings, file)
                session.delete(item)

        if not cfg.keep_summary_body and logical_item_id is not None:
            versions = session.scalars(select(SummaryVersion).where(
                SummaryVersion.logical_item_id == logical_item_id,
                SummaryVersion.summary_kind == "pending",
            )).all()
            for version in versions:
                for evidence in session.scalars(select(SummaryEvidence).where(
                    SummaryEvidence.summary_version_id == version.id
                )).all():
                    session.delete(evidence)
                session.delete(version)
            for job in session.scalars(select(SummaryJob).where(
                SummaryJob.logical_item_id == logical_item_id,
                SummaryJob.summary_kind == "pending",
            )).all():
                session.delete(job)

        _null_business_columns(occurrence)
        if delivery is not None:
            occurrence.notify_fingerprint = _fingerprint(delivery.idempotency_key)
        occurrence.cleanup_status = CLEANED
        occurrence.cleaned_at = now
        occurrence.occurrence_status = "cleaned"
        occurrence.allow_renotify = False
        session.flush()
    except Exception as exc:  # noqa: BLE001 - record and surface as cleanup_failed
        occurrence.cleanup_status = CLEANUP_FAILED
        occurrence.cleanup_error_code = f"{type(exc).__name__}"
        session.flush()
        raise

    return {
        "occurrence_id": occurrence.id,
        "cleanup_status": occurrence.cleanup_status,
        "cleaned_at": occurrence.cleaned_at.isoformat() if occurrence.cleaned_at else None,
        "notify_fingerprint": occurrence.notify_fingerprint,
        "reason": reason,
    }


def maybe_cleanup_after_delivery(
    session: Session,
    occurrence: ItemOccurrence,
    delivery: NotificationDelivery,
    settings: Settings,
    now: datetime,
) -> dict[str, Any] | None:
    """Called after a successful (or terminal) delivery. Runs cleanup if eligible.

    Honors ``cleanup_delay_hours``: when a delay is configured, the occurrence is
    marked ``pending_cleanup`` and the actual erase is deferred (the next worker
    tick or the manual action performs it). Returns the cleanup result dict, or
    ``None`` when cleanup is not applicable yet.
    """
    eligible, reason = cleanup_eligibility(occurrence, delivery, settings, now)
    if not eligible:
        return None
    delay = settings.pending_cleanup.cleanup_delay_hours
    if delay and delay > 0:
        if occurrence.cleanup_status != PENDING_CLEANUP:
            occurrence.cleanup_status = PENDING_CLEANUP
            occurrence.cleanup_requested_at = now
            session.flush()
        threshold = now - timedelta(hours=delay)
        if delivery.sent_at is None or delivery.sent_at > threshold:
            return {"deferred": True, "cleanup_status": PENDING_CLEANUP, "reason": reason}
    return perform_cleanup(session, occurrence, settings, now)
