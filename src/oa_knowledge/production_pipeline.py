"""Durable priority queues shared by realtime and historical OA pipelines."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import Integer, case, cast, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from oa_knowledge.db.models import (
    ArchivedFile, ContentObject, ItemOccurrence, OAItem, OAManifestItem,
    ParseArtifact, PipelineEvent, PipelineTask,
)


QUEUE_PRIORITY = {"realtime_pending": 0, "realtime_done": 10, "historical_done_backfill": 100}
HISTORY_CONTROL_KEY = "__historical_control__"
HISTORY_WAVE_SIZE = 50


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

    def bootstrap_current_state(self) -> dict[str, int]:
        """Seed current Pending and unparsed Done rows without sending baseline notifications."""
        created = {"realtime_pending": 0, "historical_done_backfill": 0}
        with Session(self.engine) as session:
            existing_keys = set(session.scalars(select(PipelineTask.idempotency_key)).all())
            pending = session.scalars(select(ItemOccurrence).where(
                ItemOccurrence.channel == "pending", ItemOccurrence.occurrence_status == "active",
            )).all()
            admitted = session.scalars(select(OAManifestItem).where(
                OAManifestItem.processing_status == "downloaded",
                or_(OAManifestItem.matched_exclusion_keyword.is_(None), OAManifestItem.matched_exclusion_keyword == ""),
            )).all()
            parsed_keys = set(session.scalars(
                select(OAItem.oa_item_key).join(ArchivedFile, ArchivedFile.oa_item_id == OAItem.id)
                .join(ContentObject, ContentObject.id == ArchivedFile.content_object_id)
                .join(ParseArtifact, ParseArtifact.id == ContentObject.active_parse_artifact_id)
                .where(ParseArtifact.lifecycle_status == "valid")
            ).all())
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
                if row.oa_item_key in parsed_keys:
                    continue
                key = f"history:{row.oa_item_key}:done-v1"
                if key in existing_keys:
                    continue
                session.add(PipelineTask(queue_name="historical_done_backfill", priority=QUEUE_PRIORITY["historical_done_backfill"],
                                         logical_item_key=row.oa_item_key, stage="attachment_inventory", idempotency_key=key,
                                         payload_json=json.dumps({"manifest_id": row.id})))
                existing_keys.add(key); created["historical_done_backfill"] += 1
            session.commit()
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

    def claim(self, owner: str, lease_seconds: int = 300) -> PipelineTask | None:
        now = datetime.now(timezone.utc)
        with Session(self.engine) as session:
            conditions = [PipelineTask.status == "queued", PipelineTask.idempotency_key != HISTORY_CONTROL_KEY,
                          or_(PipelineTask.next_retry_at.is_(None), PipelineTask.next_retry_at <= now)]
            if self.historical_paused(session):
                conditions.append(PipelineTask.queue_name != "historical_done_backfill")
            history_wave = case(
                (PipelineTask.queue_name == "historical_done_backfill", cast((PipelineTask.id - 1) / HISTORY_WAVE_SIZE, Integer)),
                else_=0,
            )
            history_stage = case(
                (PipelineTask.stage == "attachment_inventory", 0),
                (PipelineTask.stage == "parse", 1),
                (PipelineTask.stage == "ollama_extract", 2),
                (PipelineTask.stage == "notify_feishu", 3),
                else_=4,
            )
            row = session.scalar(select(PipelineTask).where(*conditions).order_by(
                PipelineTask.priority, history_wave, history_stage, PipelineTask.created_at, PipelineTask.id,
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

    @staticmethod
    def _owned(session: Session, task_id: int, owner: str) -> PipelineTask:
        row = session.get(PipelineTask, task_id)
        if row is None or row.status != "running" or row.lease_owner != owner:
            raise RuntimeError("pipeline task lease is not owned by this worker")
        return row
