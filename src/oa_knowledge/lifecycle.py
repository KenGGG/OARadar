"""Services for logical OA lifecycles, immutable snapshots, and summary versions."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oa_knowledge.db.models import (
    ItemOccurrence,
    ItemSnapshot,
    LogicalItem,
    SummaryEvidence,
    SummaryVersion,
)


def _canonical_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def create_logical_item(session: Session, logical_key: str, title: str) -> LogicalItem:
    existing = session.scalar(select(LogicalItem).where(LogicalItem.logical_key == logical_key))
    if existing is not None:
        return existing
    item = LogicalItem(logical_key=logical_key, title=title)
    session.add(item)
    session.flush()
    return item


def record_occurrence(
    session: Session,
    logical_item_id: int,
    occurrence_key: str,
    channel: str,
    **identifiers: str | None,
) -> ItemOccurrence:
    existing = session.scalar(select(ItemOccurrence).where(ItemOccurrence.occurrence_key == occurrence_key))
    if existing is not None:
        if existing.logical_item_id != logical_item_id:
            raise ValueError("occurrence is already linked to another logical item")
        return existing
    allowed = {
        "oa_item_id", "workitem_id_text", "process_id_text", "summary_id_text", "affair_id_text",
        "form_record_id_text", "object_id_text", "detail_url", "title", "sender",
        "occurrence_status", "raw_fields_json",
    }
    unexpected = set(identifiers) - allowed
    if unexpected:
        raise ValueError(f"unsupported occurrence fields: {sorted(unexpected)}")
    occurrence = ItemOccurrence(
        logical_item_id=logical_item_id,
        occurrence_key=occurrence_key,
        channel=channel,
        **identifiers,
    )
    session.add(occurrence)
    session.flush()
    return occurrence


def record_snapshot(
    session: Session,
    logical_item_id: int,
    occurrence_id: int | None,
    snapshot_kind: str,
    payload: dict,
    *,
    is_canonical: bool = False,
) -> ItemSnapshot:
    payload_json = _canonical_json(payload)
    content_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    existing = session.scalar(
        select(ItemSnapshot).where(
            ItemSnapshot.logical_item_id == logical_item_id,
            ItemSnapshot.snapshot_kind == snapshot_kind,
            ItemSnapshot.content_hash == content_hash,
        )
    )
    if existing is not None:
        return existing
    version = (session.scalar(
        select(func.max(ItemSnapshot.version)).where(
            ItemSnapshot.logical_item_id == logical_item_id,
            ItemSnapshot.snapshot_kind == snapshot_kind,
        )
    ) or 0) + 1
    snapshot = ItemSnapshot(
        logical_item_id=logical_item_id,
        occurrence_id=occurrence_id,
        snapshot_kind=snapshot_kind,
        version=version,
        content_hash=content_hash,
        payload_json=payload_json,
        is_canonical=is_canonical,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def create_summary_candidate(
    session: Session,
    logical_item_id: int,
    snapshot_id: int,
    summary_kind: str,
    structured: dict,
    *,
    provider_name: str,
    model_name: str,
    prompt_version: str,
) -> SummaryVersion:
    structured_json = _canonical_json(structured)
    snapshot = session.get(ItemSnapshot, snapshot_id)
    if snapshot is None or snapshot.logical_item_id != logical_item_id:
        raise ValueError("summary snapshot does not belong to logical item")
    input_hash = hashlib.sha256(
        f"{snapshot.content_hash}:{provider_name}:{model_name}:{prompt_version}:{structured_json}".encode("utf-8")
    ).hexdigest()
    version = (session.scalar(
        select(func.max(SummaryVersion.version)).where(
            SummaryVersion.logical_item_id == logical_item_id,
            SummaryVersion.summary_kind == summary_kind,
        )
    ) or 0) + 1
    candidate = SummaryVersion(
        logical_item_id=logical_item_id,
        snapshot_id=snapshot_id,
        summary_kind=summary_kind,
        version=version,
        status="candidate",
        input_hash=input_hash,
        structured_json=structured_json,
        provider_name=provider_name,
        model_name=model_name,
        prompt_version=prompt_version,
    )
    session.add(candidate)
    session.flush()
    return candidate


def validate_summary_candidate(session: Session, summary_version_id: int) -> bool:
    candidate = session.get(SummaryVersion, summary_version_id)
    if candidate is None or candidate.status != "candidate":
        return False
    try:
        structured = json.loads(candidate.structured_json)
    except (TypeError, json.JSONDecodeError):
        return False
    required = {"summary", "key_points", "attachment_summaries"}
    if not required <= set(structured) or not isinstance(structured["summary"], str):
        return False
    evidence = session.scalars(
        select(SummaryEvidence).where(SummaryEvidence.summary_version_id == candidate.id)
    ).all()
    if not evidence or any(row.snapshot_id != candidate.snapshot_id for row in evidence):
        return False

    previous = session.scalars(
        select(SummaryVersion).where(
            SummaryVersion.logical_item_id == candidate.logical_item_id,
            SummaryVersion.summary_kind == candidate.summary_kind,
            SummaryVersion.status == "valid",
            SummaryVersion.id != candidate.id,
        )
    ).all()
    for row in previous:
        row.status = "superseded"
    candidate.schema_valid = True
    candidate.status = "valid"
    logical = session.get(LogicalItem, candidate.logical_item_id)
    if candidate.summary_kind == "pending_assist":
        logical.current_pending_summary_id = candidate.id
    elif candidate.summary_kind == "done_official":
        logical.current_done_summary_id = candidate.id
    else:
        raise ValueError(f"unsupported summary kind: {candidate.summary_kind}")
    session.flush()
    return True


def mark_dependent_summaries_stale(
    session: Session,
    logical_item_id: int,
    summary_kind: str,
    current_snapshot_id: int,
) -> int:
    rows = session.scalars(
        select(SummaryVersion).where(
            SummaryVersion.logical_item_id == logical_item_id,
            SummaryVersion.summary_kind == summary_kind,
            SummaryVersion.status == "valid",
            SummaryVersion.snapshot_id != current_snapshot_id,
        )
    ).all()
    for row in rows:
        row.status = "stale"
    if rows:
        logical = session.get(LogicalItem, logical_item_id)
        if summary_kind == "pending_assist":
            logical.current_pending_summary_id = None
        elif summary_kind == "done_official":
            logical.current_done_summary_id = None
    session.flush()
    return len(rows)
