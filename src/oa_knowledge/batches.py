from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from oa_knowledge.constants import BatchStatus
from oa_knowledge.db.models import BatchItem, CollectionBatch, OAItem
from oa_knowledge.state import BATCH_TRANSITIONS, reconcile_batch, validate_transition


ALLOWED_SOURCES = {"done"}
ALLOWED_WINDOW_FIELDS = {"completed_at", "received_at"}


@dataclass(frozen=True)
class BatchPlan:
    source_channel: str
    window_start: datetime
    window_end: datetime
    window_field: str = "completed_at"
    planned_limit: int = 20
    notes: str | None = None

    def validate(self) -> None:
        if self.source_channel not in ALLOWED_SOURCES:
            raise ValueError("stage 2A supports source_channel=done only")
        if self.window_field not in ALLOWED_WINDOW_FIELDS:
            raise ValueError(f"unsupported window field: {self.window_field}")
        if self.window_start >= self.window_end:
            raise ValueError("window_start must be earlier than window_end")
        if not 1 <= self.planned_limit <= 500:
            raise ValueError("planned_limit must be between 1 and 500")
        if self.window_start.tzinfo is None or self.window_end.tzinfo is None:
            raise ValueError("batch windows must be timezone-aware")

    def canonical(self) -> dict[str, object]:
        return {
            "source_channel": self.source_channel,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "window_field": self.window_field,
            "planned_limit": self.planned_limit,
        }

    @property
    def plan_hash(self) -> str:
        payload = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    @property
    def batch_key(self) -> str:
        return f"{self.source_channel}-{self.window_start:%Y%m%d}-{self.window_end:%Y%m%d}-{self.plan_hash[:10]}"


def plan_batch(session: Session, plan: BatchPlan) -> tuple[CollectionBatch, bool]:
    plan.validate()
    existing = session.scalar(select(CollectionBatch).where(CollectionBatch.plan_hash == plan.plan_hash))
    if existing:
        return existing, False
    batch = CollectionBatch(
        batch_key=plan.batch_key,
        source_channel=plan.source_channel,
        window_start=plan.window_start,
        window_end=plan.window_end,
        window_field=plan.window_field,
        planned_limit=plan.planned_limit,
        status=BatchStatus.PLANNED,
        plan_hash=plan.plan_hash,
        notes=plan.notes,
    )
    session.add(batch)
    session.flush()
    return batch, True


def get_batch(session: Session, identifier: str) -> CollectionBatch:
    statement = select(CollectionBatch).options(selectinload(CollectionBatch.items))
    if identifier.isdigit():
        statement = statement.where(CollectionBatch.id == int(identifier))
    else:
        statement = statement.where(CollectionBatch.batch_key == identifier)
    batch = session.scalar(statement)
    if batch is None:
        raise LookupError(f"batch not found: {identifier}")
    return batch


def freeze_batch(session: Session, identifier: str) -> CollectionBatch:
    batch = get_batch(session, identifier)
    if batch.status != BatchStatus.PLANNED:
        raise ValueError("only planned batches can be frozen")
    if batch.frozen_at is None:
        batch.frozen_at = datetime.now(timezone.utc)
    return batch


def cancel_batch(session: Session, identifier: str) -> CollectionBatch:
    batch = get_batch(session, identifier)
    if batch.frozen_at is not None:
        raise ValueError("frozen batch cannot be cancelled; preserve it for audit")
    validate_transition(BatchStatus(batch.status), BatchStatus.CANCELLED, BATCH_TRANSITIONS)
    batch.status = BatchStatus.CANCELLED
    batch.finished_at = datetime.now(timezone.utc)
    return batch


def pause_batch(session: Session, identifier: str) -> CollectionBatch:
    batch = get_batch(session, identifier)
    validate_transition(BatchStatus(batch.status), BatchStatus.PAUSED, BATCH_TRANSITIONS)
    batch.status = BatchStatus.PAUSED
    return batch


def resume_batch(session: Session, identifier: str) -> CollectionBatch:
    batch = get_batch(session, identifier)
    validate_transition(BatchStatus(batch.status), BatchStatus.RUNNING, BATCH_TRANSITIONS)
    batch.status = BatchStatus.RUNNING
    return batch


def retry_batch_item(session: Session, identifier: str, ordinal: int) -> BatchItem:
    batch = get_batch(session, identifier)
    item = session.scalar(select(BatchItem).where(BatchItem.batch_id == batch.id, BatchItem.ordinal == ordinal))
    if item is None:
        raise LookupError(f"batch item ordinal not found: {ordinal}")
    if item.archive_status not in {"collect_failed", "download_failed", "review_required"}:
        raise ValueError(f"item is not retryable from status: {item.archive_status}")
    item.archive_status = "pending"
    item.last_error = None
    return item


def recover_interrupted_items(session: Session, batch_id: int) -> int:
    stale = session.scalars(
        select(BatchItem).where(BatchItem.batch_id == batch_id, BatchItem.archive_status == "archiving")
    ).all()
    for item in stale:
        item.archive_status = "pending"
        item.retry_count += 1
        item.last_error = "interrupted_before_commit"
    return len(stale)


def retry_failed_items(session: Session, identifier: str) -> int:
    batch = get_batch(session, identifier)
    failed = session.scalars(select(BatchItem).where(
        BatchItem.batch_id == batch.id,
        BatchItem.archive_status.in_({"collect_failed", "download_failed", "review_required"}),
    )).all()
    for item in failed:
        item.archive_status = "pending"
        item.last_error = None
    return len(failed)


def reuse_verified_items(session: Session, batch_id: int) -> int:
    pending = session.scalars(select(BatchItem).where(
        BatchItem.batch_id == batch_id, BatchItem.archive_status == "pending",
    )).all()
    reused = 0
    for item in pending:
        oa_item = session.scalar(select(OAItem).where(
            OAItem.oa_item_key == item.oa_item_key,
            OAItem.pipeline_status == "files_verified",
        ))
        if oa_item is None:
            continue
        prior = session.scalar(select(BatchItem).where(
            BatchItem.oa_item_key == item.oa_item_key,
            BatchItem.archive_status == "archived",
            BatchItem.archive_manifest_relpath.is_not(None),
            BatchItem.id != item.id,
        ).order_by(BatchItem.archived_at.desc()).limit(1))
        if prior is None:
            continue
        item.oa_item_id = oa_item.id
        item.detail_url = prior.detail_url
        item.archive_manifest_relpath = prior.archive_manifest_relpath
        item.archive_status = "archived"
        item.archived_at = prior.archived_at
        item.last_error = None
        reused += 1
    return reused


def apply_business_exclusions(session: Session, batch_id: int, patterns: tuple[str, ...], policy_version: str = "business-exclusions-v1") -> int:
    pending = session.scalars(select(BatchItem).where(
        BatchItem.batch_id == batch_id, BatchItem.archive_status == "pending",
    )).all()
    skipped = 0
    for item in pending:
        matched = next((pattern for pattern in patterns if pattern and pattern in item.title), None)
        if matched is None:
            continue
        item.archive_status = "confirmed_skip"
        item.skip_reason = f"excluded_title_pattern:{matched}"
        item.policy_version = policy_version
        item.last_error = None
        skipped += 1
    return skipped


def batch_dict(batch: CollectionBatch) -> dict[str, object]:
    reviewed = sum(item.archive_status == "review_required" for item in batch.items)
    unresolved = max(0, batch.discovered_count - batch.archived_count - batch.skipped_count - reviewed)
    return {
        "id": batch.id,
        "batch_key": batch.batch_key,
        "source_channel": batch.source_channel,
        "window_start": batch.window_start.isoformat() if batch.window_start else None,
        "window_end": batch.window_end.isoformat() if batch.window_end else None,
        "window_field": batch.window_field,
        "planned_limit": batch.planned_limit,
        "status": str(batch.status),
        "frozen": batch.frozen_at is not None,
        "plan_hash": batch.plan_hash,
        "counts": {
            "discovered": batch.discovered_count,
            "archived": batch.archived_count,
            "failed": batch.failed_count,
            "skipped": batch.skipped_count,
            "reviewed": reviewed,
            "unresolved": unresolved,
        },
        "source": {
            "total_count": batch.source_total_count,
            "total_pages": batch.source_total_pages,
            "pages_scanned": batch.pages_scanned,
            "query_count": batch.query_count,
            "scanned_row_count": batch.scanned_row_count,
            "filtered_or_duplicate_count": max(0, batch.scanned_row_count - batch.query_count),
        },
        "items": len(batch.items),
    }


def apply_discovery(session: Session, batch: CollectionBatch, discovered_items) -> CollectionBatch:
    if batch.frozen_at is None:
        raise ValueError("batch must be frozen before discovery")
    if batch.status == BatchStatus.READY:
        return batch
    validate_transition(BatchStatus(batch.status), BatchStatus.DISCOVERING, BATCH_TRANSITIONS)
    batch.status = BatchStatus.DISCOVERING
    batch.started_at = batch.started_at or datetime.now(timezone.utc)
    accepted = []
    for item in discovered_items:
        timestamp = item.completed_at if batch.window_field == "completed_at" else item.created_at
        if timestamp is None:
            continue
        naive_start = batch.window_start.replace(tzinfo=None) if batch.window_start else None
        naive_end = batch.window_end.replace(tzinfo=None) if batch.window_end else None
        if naive_start and timestamp < naive_start or naive_end and timestamp >= naive_end:
            continue
        accepted.append(item)
        if len(accepted) >= batch.planned_limit:
            break
    for ordinal, item in enumerate(accepted, 1):
        session.add(
            BatchItem(
                batch_id=batch.id,
                oa_item_key=item.oa_item_key,
                workitem_id_text=item.workitem_id_text,
                title=item.title,
                created_at=item.created_at,
                completed_at=item.completed_at,
                sender=item.sender,
                deadline_text=item.deadline_text,
                category=item.category,
                ordinal=ordinal,
                list_page=item.list_page,
                discovery_status="discovered",
            )
        )
    batch.discovered_count = len(accepted)
    batch.status = BatchStatus.READY
    session.flush()
    return batch


@dataclass(frozen=True)
class ValidationReport:
    batch_key: str
    batch_id: int
    discovered: int
    archived: int
    failed: int
    skipped: int
    reviewed: int
    unresolved: int
    reconciled: bool
    query_count: int | None
    source_match: bool
    status: str


def validate_batch(session: Session, identifier: str) -> ValidationReport:
    """Reconcile a completed or paused batch against its manifest and source counts.

    Per the 2A-7 gate: ``OA query count = archived + confirmed_skip + review_required + unresolved``.
    ``unresolved`` must be 0 for the batch to be considered reconciled.
    """
    batch = get_batch(session, identifier)
    total_items = len(batch.items)
    archived = session.scalar(
        select(func.count()).select_from(BatchItem).where(
            BatchItem.batch_id == batch.id, BatchItem.archive_status == "archived",
        )
    ) or 0
    failed = session.scalar(
        select(func.count()).select_from(BatchItem).where(
            BatchItem.batch_id == batch.id, BatchItem.archive_status.in_({"collect_failed", "download_failed"}),
        )
    ) or 0
    skipped = batch.skipped_count
    reviewed = session.scalar(
        select(func.count()).select_from(BatchItem).where(
            BatchItem.batch_id == batch.id, BatchItem.archive_status == "review_required",
        )
    ) or 0
    unresolved = max(0, batch.discovered_count - archived - skipped - reviewed)
    reconciled = reconcile_batch(batch.discovered_count, archived, skipped, unresolved, reviewed)
    source_match = True
    if batch.query_count is not None:
        source_match = batch.query_count == batch.discovered_count or (
            batch.query_count >= batch.discovered_count
            and batch.scanned_row_count == batch.query_count
            and batch.source_total_count == batch.query_count
        )
    return ValidationReport(
        batch_key=batch.batch_key,
        batch_id=batch.id,
        discovered=batch.discovered_count,
        archived=archived,
        failed=failed,
        skipped=skipped,
        reviewed=reviewed,
        unresolved=unresolved,
        reconciled=reconciled,
        query_count=batch.query_count,
        source_match=source_match,
        status=batch.status,
    )
