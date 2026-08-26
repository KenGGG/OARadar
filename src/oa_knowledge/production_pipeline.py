"""Durable priority queues shared by realtime and historical OA pipelines."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import Integer, and_, case, cast, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from oa_knowledge.constants import LEASE_TTL
from oa_knowledge.archive_migration_campaign import SAFE_AUDIT_STATUSES, SAFE_COMPARISON_REASONS
from oa_knowledge.db.models import (
    ArchivedFile, ContentObject, CuratedRun, ItemOccurrence, OAItem, OAManifestItem,
    OnlineAuditItem, OnlineAuditRun, ParseArtifact, PipelineEvent, PipelineTask,
)
from oa_knowledge.source_roles import MARKDOWN_SOURCE_ROLES


QUEUE_PRIORITY = {
    "realtime_pending": 0,
    "realtime_done": 10,
    "markdown_delivery": 50,
    "historical_done_backfill": 100,
}
HISTORY_CONTROL_KEY = "__historical_control__"
HISTORY_WAVE_SIZE = 50
DEFAULT_TASK_LEASE_SECONDS = int(LEASE_TTL.total_seconds())

# V2 only admits stages belonging to the three production chains.  Old rows
# remain in the database for diagnosis, but must never be resumed or executed.
CORE_PIPELINE_STAGES = frozenset({
    "detail_sync", "pending_parse", "pending_summary", "notify_feishu",
    "pending_cleanup", "oa_resync", "done_capture_and_archive", "archive_verify",
    "attachment_inventory", "parse", "source_publish", "classify", "index_publish",
})


class ProductionQueue:
    def __init__(self, engine) -> None:
        self.engine = engine

    def enqueue(self, queue_name: str, logical_item_key: str, stage: str, idempotency_key: str, *, payload: dict | None = None) -> int:
        if queue_name not in QUEUE_PRIORITY:
            raise ValueError(f"unsupported queue: {queue_name}")
        with Session(self.engine) as session:
            existing = session.scalar(select(PipelineTask).where(PipelineTask.idempotency_key == idempotency_key))
            if existing:
                return existing.id
            task = PipelineTask(queue_name=queue_name, priority=QUEUE_PRIORITY[queue_name], logical_item_key=logical_item_key,
                                stage=stage, idempotency_key=idempotency_key, payload_json=json.dumps(payload or {}, ensure_ascii=False))
            session.add(task)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return session.scalar(select(PipelineTask.id).where(PipelineTask.idempotency_key == idempotency_key))
            return task.id

    def retire_non_core_tasks(self) -> int:
        """Park queued legacy stages without deleting their diagnostic history."""
        now = datetime.now(timezone.utc)
        with Session(self.engine) as session:
            rows = session.scalars(select(PipelineTask).where(
                PipelineTask.status == "queued",
                ~PipelineTask.stage.in_(CORE_PIPELINE_STAGES),
            )).all()
            for row in rows:
                row.status = "failed"
                row.recoverable = False
                row.error_code = "RETIRED_STAGE"
                row.last_error = "stage retired from OARadar V2 production pipelines"
                row.next_retry_at = None
                row.finished_at = now
                row.lease_owner = None
                row.lease_expires_at = None
                session.add(PipelineEvent(
                    task_id=row.id,
                    event_type="retired",
                    stage=row.stage,
                    status="failed",
                    details_json=json.dumps({"error_code": "RETIRED_STAGE"}),
                ))
            session.commit()
            return len(rows)

    def enqueue_stale_curation(
        self, *, rules_version: str, prompt_version: str, schema_version: str,
    ) -> int:
        """Defer version-changed re-curation to the historical queue.

        A completed realtime task is immutable evidence of that attempt. A new
        version receives its own idempotent historical task, so online audit and
        future realtime captures retain priority.
        """
        target = (rules_version, prompt_version, schema_version)
        created = 0
        with Session(self.engine) as session:
            latest_ids = list(session.scalars(
                select(func.max(CuratedRun.id))
                .where(CuratedRun.status.in_(("completed", "needs_review", "failed")))
                .group_by(CuratedRun.logical_item_id)
            ))
            if not latest_ids:
                return 0
            runs = session.scalars(select(CuratedRun).where(CuratedRun.id.in_(latest_ids))).all()
            for run in runs:
                if (run.rules_version, run.prompt_version, run.schema_version) == target:
                    continue
                item = session.scalar(select(OAItem).where(
                    OAItem.logical_item_id == run.logical_item_id,
                    OAItem.source_channel == "done",
                ).order_by(OAItem.id).limit(1))
                if item is None:
                    continue
                active = session.scalar(select(PipelineTask.id).where(
                    PipelineTask.queue_name == "historical_done_backfill",
                    PipelineTask.logical_item_key == item.oa_item_key,
                    PipelineTask.status.in_(("queued", "running")),
                ).limit(1))
                if active is not None:
                    continue
                idempotency_key = (
                    f"recurate:{run.logical_item_id}:{rules_version}:{prompt_version}:{schema_version}"
                )
                if session.scalar(select(PipelineTask.id).where(
                    PipelineTask.idempotency_key == idempotency_key,
                )) is not None:
                    continue
                session.add(PipelineTask(
                    queue_name="historical_done_backfill",
                    priority=QUEUE_PRIORITY["historical_done_backfill"],
                    logical_item_key=item.oa_item_key,
                    stage="curation",
                    idempotency_key=idempotency_key,
                    payload_json=json.dumps({"reason": "curation_version_changed"}),
                ))
                created += 1
            session.commit()
        return created

    def bootstrap_current_state(
        self, *, session: Session | None = None, include_pending: bool = True,
    ) -> dict[str, int]:
        """Seed Pending and resume every admitted Done item in the unified pipeline.

        A caller-owned session keeps scheduled manifest sync and queue seeding in
        one SQLite transaction. Without one, this method owns and commits it.
        """
        if session is not None:
            return self._bootstrap_session(session, include_pending=include_pending)
        with Session(self.engine) as owned:
            created = self._bootstrap_session(owned, include_pending=include_pending)
            owned.commit()
            return created

    def start_historical_rebuild(self) -> dict[str, int]:
        """Start an explicit Done rebuild, or safely resume the active campaign.

        Scheduled bootstrap remains idempotent. An explicit user start may replay
        a finished campaign so deleted derivatives can be regenerated, but it
        never resets tasks while any historical work is queued or running.
        """
        with Session(self.engine) as session:
            campaign = PipelineTask.idempotency_key.like("history:%:knowledge-v2")
            already_active = session.scalar(select(func.count(PipelineTask.id)).where(
                PipelineTask.queue_name == "historical_done_backfill",
                campaign,
                PipelineTask.status.in_(("queued", "running")),
            )) or 0
            created = self._bootstrap_session(session, include_pending=False)["historical_done_backfill"]
            reset_values = dict(
                stage="attachment_inventory", status="queued", attempts=0,
                progress_current=0, progress_total=None, error_code=None,
                last_error=None, recoverable=True, next_retry_at=None,
                started_at=None, finished_at=None, lease_owner=None,
                lease_expires_at=None,
            )
            legacy = session.execute(
                update(PipelineTask).where(
                    PipelineTask.queue_name == "historical_done_backfill",
                    campaign,
                    or_(
                        PipelineTask.status.in_(("queued", "completed")),
                        and_(
                            PipelineTask.status == "failed",
                            PipelineTask.recoverable.is_(True),
                        ),
                    ),
                    PipelineTask.stage == "ollama_extract",
                ).values(**reset_values)
            )
            repaired_legacy = legacy.rowcount
            requeued = 0
            if already_active == 0:
                result = session.execute(
                    update(PipelineTask).where(
                        PipelineTask.queue_name == "historical_done_backfill",
                        campaign,
                        or_(
                            PipelineTask.status == "completed",
                            and_(
                                PipelineTask.status == "failed",
                                PipelineTask.recoverable.is_(True),
                            ),
                        ),
                    ).values(**reset_values)
                )
                requeued = result.rowcount
            session.commit()
            return {
                "created": created,
                "requeued": requeued,
                "repaired_legacy": repaired_legacy,
                "already_active": int(already_active),
            }

    def finalize_ineligible_historical_tasks(
        self, audit_run_id: int, *, session: Session | None = None,
    ) -> int:
        """Park queued Done rebuild tasks that lack safe, canonical evidence.

        This is called only after the verified archive migration has exhausted
        its eligible rows. It changes queue state, never OA source bytes. A
        later audit/migration campaign may explicitly create a new safe task;
        ordinary retry and rebuild controls cannot bypass this review gate.
        """
        now = datetime.now(timezone.utc)
        owned = Session(self.engine) if session is None else None
        db_session = owned or session
        try:
            audit = db_session.get(OnlineAuditRun, audit_run_id)
            if audit is None or audit.status != "completed":
                raise ValueError("completed online audit is required")
            safe_keys = select(OnlineAuditItem.oa_item_key).where(
                OnlineAuditItem.run_id == audit_run_id,
                OnlineAuditItem.status.in_(SAFE_AUDIT_STATUSES),
                OnlineAuditItem.comparison_reason.in_(SAFE_COMPARISON_REASONS),
                OnlineAuditItem.depth_limit_reached.is_(False),
            )
            canonical_keys = select(OAItem.oa_item_key).where(
                OAItem.source_channel == "done",
                OAItem.archive_relpath.like("originals/%"),
            )
            rows = db_session.scalars(select(PipelineTask).where(
                PipelineTask.queue_name == "historical_done_backfill",
                PipelineTask.status == "queued",
                PipelineTask.idempotency_key != HISTORY_CONTROL_KEY,
                ~and_(
                    PipelineTask.logical_item_key.in_(safe_keys),
                    PipelineTask.logical_item_key.in_(canonical_keys),
                ),
            )).all()
            for row in rows:
                row.status = "failed"
                row.error_code = "ONLINE_AUDIT_REVIEW_REQUIRED"
                row.last_error = "在线核验或安全迁移未通过，需人工复核后重新核验"
                row.recoverable = False
                row.next_retry_at = None
                row.finished_at = now
                row.lease_owner = None
                row.lease_expires_at = None
                db_session.add(PipelineEvent(
                    task_id=row.id,
                    event_type="review_required",
                    stage=row.stage,
                    status="failed",
                    details_json=json.dumps({
                        "error_code": "ONLINE_AUDIT_REVIEW_REQUIRED",
                        "audit_run_id": audit_run_id,
                    }),
                ))
            if owned is not None:
                owned.commit()
            else:
                db_session.flush()
            return len(rows)
        finally:
            if owned is not None:
                owned.close()

    def release_verified_historical_tasks(
        self, audit_run_id: int, *, session: Session | None = None,
    ) -> int:
        """Release only review-gated tasks proven safe by a later audit."""
        owned = Session(self.engine) if session is None else None
        db_session = owned or session
        try:
            audit = db_session.get(OnlineAuditRun, audit_run_id)
            if audit is None or audit.status != "completed":
                raise ValueError("completed online audit is required")
            safe_keys = select(OnlineAuditItem.oa_item_key).where(
                OnlineAuditItem.run_id == audit_run_id,
                OnlineAuditItem.status.in_(SAFE_AUDIT_STATUSES),
                OnlineAuditItem.comparison_reason.in_(SAFE_COMPARISON_REASONS),
                OnlineAuditItem.depth_limit_reached.is_(False),
            )
            canonical_keys = select(OAItem.oa_item_key).where(
                OAItem.source_channel == "done",
                OAItem.archive_relpath.like("originals/%"),
            )
            rows = db_session.scalars(select(PipelineTask).where(
                PipelineTask.queue_name == "historical_done_backfill",
                PipelineTask.status == "failed",
                PipelineTask.recoverable.is_(False),
                PipelineTask.error_code == "ONLINE_AUDIT_REVIEW_REQUIRED",
                PipelineTask.logical_item_key.in_(safe_keys),
                PipelineTask.logical_item_key.in_(canonical_keys),
            )).all()
            for row in rows:
                row.stage = "attachment_inventory"
                row.status = "queued"
                row.progress_current = 0
                row.progress_total = None
                row.attempts = 0
                row.error_code = None
                row.last_error = None
                row.recoverable = True
                row.next_retry_at = None
                row.started_at = None
                row.finished_at = None
                row.lease_owner = None
                row.lease_expires_at = None
                db_session.add(PipelineEvent(
                    task_id=row.id,
                    event_type="review_gate_released",
                    stage=row.stage,
                    status="queued",
                    details_json=json.dumps({"audit_run_id": audit_run_id}),
                ))
            if owned is not None:
                owned.commit()
            else:
                db_session.flush()
            return len(rows)
        finally:
            if owned is not None:
                owned.close()

    @staticmethod
    def _bootstrap_session(session: Session, *, include_pending: bool = True) -> dict[str, int]:
        created = {"realtime_pending": 0, "historical_done_backfill": 0}
        existing_keys = set(session.scalars(select(PipelineTask.idempotency_key)).all())
        pending = session.scalars(select(ItemOccurrence).where(
            ItemOccurrence.channel == "pending", ItemOccurrence.occurrence_status == "active",
        )).all()
        admitted = session.scalars(select(OAManifestItem).where(
            OAManifestItem.processing_status == "downloaded",
            or_(OAManifestItem.matched_exclusion_keyword.is_(None), OAManifestItem.matched_exclusion_keyword == ""),
        )).all()
        source_conditions = (
            ArchivedFile.file_role.in_(MARKDOWN_SOURCE_ROLES),
            ArchivedFile.download_status == "verified",
            ArchivedFile.local_relpath.is_not(None),
        )
        source_counts = dict(session.execute(
            select(OAItem.oa_item_key, func.count(ArchivedFile.id))
            .join(ArchivedFile, ArchivedFile.oa_item_id == OAItem.id)
            .where(*source_conditions)
            .group_by(OAItem.oa_item_key)
        ).all())
        valid_counts = dict(session.execute(
            select(OAItem.oa_item_key, func.count(ArchivedFile.id))
            .join(ArchivedFile, ArchivedFile.oa_item_id == OAItem.id)
            .join(ContentObject, ContentObject.id == ArchivedFile.content_object_id)
            .join(ParseArtifact, ParseArtifact.id == ContentObject.active_parse_artifact_id)
            .where(*source_conditions, ParseArtifact.lifecycle_status == "valid")
            .group_by(OAItem.oa_item_key)
        ).all())
        if include_pending:
            for row in pending:
                key = f"pending:{row.occurrence_key}:{row.discovery_hash or 'baseline'}:detail-v1"
                if key in existing_keys:
                    continue
                session.add(PipelineTask(queue_name="realtime_pending", priority=QUEUE_PRIORITY["realtime_pending"],
                                         logical_item_key=str(row.logical_item_id), logical_item_id=row.logical_item_id,
                                         stage="detail_sync", idempotency_key=key,
                                         max_attempts=5,
                                         payload_json=json.dumps({"occurrence_id": row.id, "baseline": True, "notify": False})))
                existing_keys.add(key); created["realtime_pending"] += 1
        for row in admitted:
            key = f"history:{row.oa_item_key}:knowledge-v2"
            if key in existing_keys:
                continue
            source_count = source_counts.get(row.oa_item_key, 0)
            stage = "source_publish" if source_count and valid_counts.get(row.oa_item_key, 0) == source_count else "attachment_inventory"
            session.add(PipelineTask(queue_name="historical_done_backfill", priority=QUEUE_PRIORITY["historical_done_backfill"],
                                     logical_item_key=row.oa_item_key, stage=stage, idempotency_key=key,
                                     payload_json=json.dumps({"manifest_id": row.id})))
            existing_keys.add(key); created["historical_done_backfill"] += 1
        session.flush()
        return created

    def _find_id(self, idempotency_key: str) -> int | None:
        with Session(self.engine) as session:
            return session.scalar(select(PipelineTask.id).where(PipelineTask.idempotency_key == idempotency_key))

    def set_historical_paused(self, paused: bool) -> None:
        with Session(self.engine) as session:
            row = session.scalar(select(PipelineTask).where(PipelineTask.idempotency_key == HISTORY_CONTROL_KEY))
            if row is None:
                row = PipelineTask(queue_name="historical_done_backfill", priority=100, logical_item_key=HISTORY_CONTROL_KEY,
                                   stage="control", idempotency_key=HISTORY_CONTROL_KEY, status="paused" if paused else "completed", recoverable=False)
                session.add(row)
            else:
                row.status = "paused" if paused else "completed"
            session.commit()

    def historical_paused(self, session: Session | None = None) -> bool:
        if session is None:
            with Session(self.engine) as owned:
                return self.historical_paused(owned)
        status = session.scalar(select(PipelineTask.status).where(PipelineTask.idempotency_key == HISTORY_CONTROL_KEY))
        return status == "paused"

    def claim(
        self,
        owner: str,
        lease_seconds: int = 300,
        *,
        queue_names: tuple[str, ...] | None = None,
    ) -> PipelineTask | None:
        self.retire_non_core_tasks()
        now = datetime.now(timezone.utc)
        with Session(self.engine) as session:
            conditions = [PipelineTask.status == "queued", PipelineTask.idempotency_key != HISTORY_CONTROL_KEY,
                          or_(PipelineTask.next_retry_at.is_(None), PipelineTask.next_retry_at <= now)]
            if queue_names is not None:
                conditions.append(PipelineTask.queue_name.in_(queue_names))
            if self.historical_paused(session):
                conditions.append(PipelineTask.queue_name != "historical_done_backfill")
            # Only the newest completed online audit may authorize history. An
            # older completed run becomes stale as soon as a newer run is
            # queued/running/reopened, and first deploy remains fail-closed.
            canonical_done_keys = select(OAItem.oa_item_key).where(
                OAItem.source_channel == "done",
                OAItem.archive_relpath.like("originals/%"),
            )
            latest_audit_id = select(func.max(OnlineAuditRun.id)).scalar_subquery()
            latest_audit_completed = exists(select(OnlineAuditRun.id).where(
                OnlineAuditRun.id == latest_audit_id,
                OnlineAuditRun.status == "completed",
            ))
            verified_done_keys = select(OnlineAuditItem.oa_item_key).where(
                OnlineAuditItem.run_id == latest_audit_id,
                OnlineAuditItem.status.in_(SAFE_AUDIT_STATUSES),
                OnlineAuditItem.comparison_reason.in_(SAFE_COMPARISON_REASONS),
                OnlineAuditItem.depth_limit_reached.is_(False),
            )
            conditions.append(or_(
                PipelineTask.queue_name != "historical_done_backfill",
                and_(
                    latest_audit_completed,
                    PipelineTask.logical_item_key.in_(canonical_done_keys),
                    PipelineTask.logical_item_key.in_(verified_done_keys),
                ),
            ))
            history_wave = case(
                (PipelineTask.queue_name == "historical_done_backfill", cast((PipelineTask.id - 1) / HISTORY_WAVE_SIZE, Integer)),
                else_=0,
            )
            history_stage = case(
                (PipelineTask.stage == "attachment_inventory", 0),
                (PipelineTask.stage == "parse", 1),
                (PipelineTask.stage == "source_publish", 2),
                (PipelineTask.stage == "curation", 3),
                (PipelineTask.stage == "ollama_extract", 4),
                (PipelineTask.stage == "notify_feishu", 5),
                else_=6,
            )
            realtime_done_stage = case(
                (
                    (PipelineTask.queue_name == "realtime_done")
                    & (PipelineTask.stage == "done_capture_and_archive"),
                    0,
                ),
                (
                    (PipelineTask.queue_name == "realtime_done")
                    & (PipelineTask.stage == "attachment_inventory"),
                    1,
                ),
                (
                    (PipelineTask.queue_name == "realtime_done")
                    & (PipelineTask.stage == "parse"),
                    2,
                ),
                (
                    (PipelineTask.queue_name == "realtime_done")
                    & (PipelineTask.stage == "source_publish"),
                    3,
                ),
                (
                    (PipelineTask.queue_name == "realtime_done")
                    & (PipelineTask.stage.in_(("curation", "ollama_extract"))),
                    4,
                ),
                else_=0,
            )
            fair_order = case(
                (PipelineTask.queue_name.in_(("realtime_pending", "realtime_done")), PipelineTask.updated_at),
                else_=PipelineTask.created_at,
            )
            row = session.scalar(select(PipelineTask).where(*conditions).order_by(
                PipelineTask.priority, realtime_done_stage, history_wave,
                history_stage, fair_order, PipelineTask.id,
            ).limit(1))
            if row is None:
                return None
            row.status = "running"; row.lease_owner = owner; row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            row.started_at = row.started_at or now; row.attempts += 1
            session.add(PipelineEvent(task_id=row.id, event_type="claimed", stage=row.stage, status="running"))
            session.commit(); session.refresh(row); session.expunge(row)
            return row

    def recover_abandoned(self, owner_alive) -> int:
        now = datetime.now(timezone.utc); recovered = 0
        with Session(self.engine) as session:
            rows = session.scalars(select(PipelineTask).where(PipelineTask.status == "running")).all()
            for row in rows:
                expires = row.lease_expires_at
                if expires and expires.tzinfo is None: expires = expires.replace(tzinfo=timezone.utc)
                if (expires is None or expires <= now) or not owner_alive(row.lease_owner):
                    row.status = "queued"; row.lease_owner = None; row.lease_expires_at = None
                    session.add(PipelineEvent(task_id=row.id,event_type="recovered",stage=row.stage,status="queued")); recovered += 1
            session.commit()
        return recovered

    def heartbeat(self, task_id: int, owner: str, lease_seconds: int = DEFAULT_TASK_LEASE_SECONDS) -> bool:
        """Extend an owned running task lease without changing business state."""
        now = datetime.now(timezone.utc)
        with Session(self.engine) as session:
            row = session.scalar(select(PipelineTask).where(
                PipelineTask.id == task_id,
                PipelineTask.status == "running",
                PipelineTask.lease_owner == owner,
            ))
            if row is None:
                return False
            row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            session.commit()
            return True

    def advance(self, task_id: int, owner: str, stage: str, *, progress_current: int = 0, progress_total: int | None = None) -> None:
        with Session(self.engine) as session:
            row = self._owned(session, task_id, owner)
            row.stage = stage; row.status = "queued"; row.progress_current = progress_current; row.progress_total = progress_total
            row.attempts = 0
            row.error_code = None; row.last_error = None; row.next_retry_at = None
            row.lease_owner = None; row.lease_expires_at = None
            session.add(PipelineEvent(task_id=row.id, event_type="stage_advanced", stage=stage, status="queued"))
            session.commit()

    def complete(self, task_id: int, owner: str) -> None:
        with Session(self.engine) as session:
            row = self._owned(session, task_id, owner)
            row.status = "completed"; row.finished_at = datetime.now(timezone.utc); row.lease_owner = None; row.lease_expires_at = None
            row.error_code = None; row.last_error = None; row.next_retry_at = None
            session.add(PipelineEvent(task_id=row.id, event_type="completed", stage=row.stage, status="completed"))
            session.commit()

    def fail(self, task_id: int, owner: str, error_code: str, detail: str, *, recoverable: bool = True) -> None:
        with Session(self.engine) as session:
            row = self._owned(session, task_id, owner)
            can_retry = recoverable and row.attempts < row.max_attempts
            row.status = "queued" if can_retry else "failed"; row.error_code = error_code; row.last_error = detail[:1000]; row.recoverable = recoverable
            row.next_retry_at = datetime.now(timezone.utc) + timedelta(minutes=min(30, 2 ** row.attempts)) if can_retry else None
            row.finished_at = None if can_retry else datetime.now(timezone.utc); row.lease_owner = None; row.lease_expires_at = None
            session.add(PipelineEvent(task_id=row.id, event_type="retry_scheduled" if can_retry else "failed", stage=row.stage, status=row.status,
                                      details_json=json.dumps({"error_code": error_code, "recoverable": recoverable})))
            session.commit()

    def retry_failed(self) -> int:
        """Retry only recoverable failures; review-gated tasks need a scoped action."""
        with Session(self.engine) as session:
            result = session.execute(
                update(PipelineTask).where(
                    PipelineTask.status == "failed",
                    PipelineTask.recoverable.is_(True),
                    PipelineTask.idempotency_key != HISTORY_CONTROL_KEY,
                ).values(
                    status="queued", attempts=0, error_code=None, last_error=None,
                    recoverable=True, next_retry_at=None, finished_at=None,
                    lease_owner=None, lease_expires_at=None,
                )
            )
            session.commit()
            return result.rowcount

    @staticmethod
    def _owned(session: Session, task_id: int, owner: str) -> PipelineTask:
        row = session.get(PipelineTask, task_id)
        if row is None or row.status != "running" or row.lease_owner != owner:
            raise RuntimeError("pipeline task lease is not owned by this worker")
        return row
