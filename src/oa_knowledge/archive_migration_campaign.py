"""核验完成后，把已证明安全的 legacy 已办原件迁移到统一目录。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oa_knowledge.db.models import (
    OAItem,
    OAManifestItem,
    OAManifestSync,
    OnlineAuditItem,
    OnlineAuditRun,
    OperationEvent,
    OperationJob,
    PipelineTask,
)


SAFE_AUDIT_STATUSES = frozenset({"matched", "historical_retained"})
SAFE_COMPARISON_REASONS = frozenset({"exact_match", "historical_retained"})
MIGRATION_VERSION = "verified-archive-path-v1"


def full_manifest_reconciliation_is_current(
    session: Session,
    audit: OnlineAuditRun,
) -> bool:
    """Require a complete full-page OA manifest sync from this audit window."""
    latest = session.scalar(
        select(OAManifestSync).order_by(OAManifestSync.id.desc()).limit(1)
    )
    if latest is None or latest.status != "manifest_complete":
        return False
    current_count = int(session.scalar(
        select(func.count()).select_from(OAManifestItem)
    ) or 0)
    anchor = audit.started_at or audit.created_at

    def utc_naive(value: datetime | None) -> datetime | None:
        if value is None or value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    return bool(
        latest.finished_at
        and utc_naive(latest.finished_at) >= utc_naive(anchor)
        and latest.pages_scanned == latest.source_total_pages
        and latest.oa_total_count == current_count
        and latest.local_manifest_count == current_count
        and audit.total_items == current_count
    )


def eligible_legacy_done_ids(
    session: Session,
    audit_run_id: int,
    *,
    exclude_ids: set[int] | None = None,
    limit: int | None = None,
) -> list[int]:
    """Return local row IDs only; OA identifiers never enter the job payload."""
    query = (
        select(OAItem.id)
        .join(
            OnlineAuditItem,
            (OnlineAuditItem.run_id == audit_run_id)
            & (OnlineAuditItem.oa_item_key == OAItem.oa_item_key),
        )
        .where(
            OAItem.source_channel == "done",
            OAItem.archive_relpath.like("raw/done/%"),
            OnlineAuditItem.status.in_(SAFE_AUDIT_STATUSES),
            OnlineAuditItem.comparison_reason.in_(SAFE_COMPARISON_REASONS),
            OnlineAuditItem.depth_limit_reached.is_(False),
        )
        .order_by(OAItem.id)
    )
    if exclude_ids:
        query = query.where(OAItem.id.not_in(exclude_ids))
    if limit is not None:
        query = query.limit(limit)
    return list(session.scalars(query))


def migration_review_count(session: Session, audit_run_id: int) -> int:
    return int(session.scalar(
        select(func.count(OAItem.id))
        .join(
            OnlineAuditItem,
            (OnlineAuditItem.run_id == audit_run_id)
            & (OnlineAuditItem.oa_item_key == OAItem.oa_item_key),
        )
        .where(
            OAItem.source_channel == "done",
            OAItem.archive_relpath.like("raw/done/%"),
            (
                OnlineAuditItem.status.not_in(SAFE_AUDIT_STATUSES)
                | OnlineAuditItem.comparison_reason.not_in(SAFE_COMPARISON_REASONS)
                | OnlineAuditItem.comparison_reason.is_(None)
                | OnlineAuditItem.depth_limit_reached.is_(True)
            ),
        )
    ) or 0)


def ensure_verified_archive_migration(engine) -> int | None:
    """Create one durable migration job only after a full audit completed."""
    with Session(engine) as session:
        audit = session.scalar(
            select(OnlineAuditRun).order_by(OnlineAuditRun.id.desc()).limit(1)
        )
        if audit is not None and audit.status == "completed":
            from oa_knowledge.online_audit import enroll_new_manifest_items
            if enroll_new_manifest_items(session, audit):
                session.commit()
                return None
        if (
            audit is None
            or audit.status != "completed"
            or not full_manifest_reconciliation_is_current(session, audit)
            or not eligible_legacy_done_ids(session, audit.id, limit=1)
        ):
            return None
        active_supplement = session.scalar(select(PipelineTask.id).where(
            PipelineTask.queue_name == "realtime_done",
            PipelineTask.status.in_(("queued", "running")),
            PipelineTask.idempotency_key.like(f"online-audit:{audit.id}:%"),
        ).limit(1))
        if active_supplement is not None:
            return None
        idempotency_key = f"archive-migration:{audit.id}:{MIGRATION_VERSION}"
        existing = session.scalar(select(OperationJob).where(
            OperationJob.idempotency_key == idempotency_key,
        ))
        if existing is not None:
            if (
                existing.status == "paused"
                and existing.last_error_code == "WAITING_FOR_ONLINE_AUDIT"
            ):
                existing.status = "queued"
                existing.last_error_code = None
                existing.heartbeat_at = None
                existing.lease_owner = None
                existing.lease_expires_at = None
                session.commit()
            return existing.id
        total = len(eligible_legacy_done_ids(session, audit.id))
        job = OperationJob(
            job_key=f"archive-migration-{audit.id}",
            job_type="verified_archive_migration",
            status="queued",
            idempotency_key=idempotency_key,
            progress_total=total,
            parameters_json=json.dumps({
                "audit_run_id": audit.id,
                "migration_version": MIGRATION_VERSION,
                "processed": 0,
                "migrated": 0,
                "failed": 0,
                "failed_item_ids": [],
            }, sort_keys=True),
        )
        job.events.append(OperationEvent(
            sequence=1,
            event_type="verified_archive_migration_created",
            status="queued",
            details_json=json.dumps({"target_count": total}),
        ))
        session.add(job)
        session.commit()
        return job.id
