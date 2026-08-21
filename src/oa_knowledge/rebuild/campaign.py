"""Small, synchronous local orchestration for clean archive rebuilds."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.models import (
    PipelineEvent,
    PipelineRun,
    PipelineTask,
    RebuildOutput,
)
from oa_knowledge.rebuild.archive_copy import copy_inventory_row
from oa_knowledge.rebuild.inventory import InventoryRow

QUEUE_NAME = "data_rebuild"
ARCHIVE_STAGE = "archive_copy"
INVENTORY_STAGE = "inventory"
RESUMABLE_RUN_STATUSES = frozenset({"running", "failed"})


def create_rebuild_run(session: Session, *, cutoff_at: datetime) -> PipelineRun:
    """Persist a new local rebuild run with an explicit, aware cutoff instant."""
    if cutoff_at.tzinfo is None or cutoff_at.utcoffset() is None:
        raise ValueError("cutoff_at must be timezone-aware")
    run = PipelineRun(
        run_key=f"rebuild:{cutoff_at.astimezone(UTC).strftime('%Y%m%dT%H%M%S%fZ')}:{uuid4().hex}",
        pipeline_type="data_rebuild",
        status="running",
    )
    session.add(run)
    session.commit()
    return run


def _add_event(session: Session, task: PipelineTask, event_type: str, status: str, *, error_code: str | None = None) -> None:
    details = {} if error_code is None else {"error_code": error_code}
    session.add(PipelineEvent(
        task_id=task.id, event_type=event_type, stage=task.stage, status=status,
        details_json=json.dumps(details, sort_keys=True),
    ))


def resume_rebuild_run(session: Session, run_id: int) -> PipelineRun:
    """Return only an unfinished/recoverable local rebuild run for CLI resume."""
    run = session.get(PipelineRun, run_id)
    if run is None or run.pipeline_type != "data_rebuild":
        raise ValueError("REBUILD_RUN_NOT_FOUND")
    if run.status not in RESUMABLE_RUN_STATUSES:
        raise ValueError("REBUILD_RUN_NOT_RESUMABLE")
    return run


def _mark_task_completed(session: Session, task: PipelineTask) -> bool:
    """Reconcile a durable successful output to one completed task event."""
    changed = task.status != "completed"
    task.status = "completed"
    task.error_code = task.last_error = None
    task.finished_at = task.finished_at or datetime.now(UTC)
    task.lease_owner = task.lease_expires_at = None
    completed_event = session.scalar(select(PipelineEvent.id).where(
        PipelineEvent.task_id == task.id,
        PipelineEvent.event_type == "completed",
    ))
    if completed_event is None:
        _add_event(session, task, "completed", "completed")
    return changed


def _success_output(session: Session, run_id: int, row: InventoryRow) -> RebuildOutput | None:
    return session.scalar(select(RebuildOutput).where(
        RebuildOutput.run_id == run_id,
        RebuildOutput.source_file_id == row.file_id,
        RebuildOutput.sha256 == row.sha256,
        RebuildOutput.status == "success",
    ))


def _lease_expired(task: PipelineTask) -> bool:
    expires = task.lease_expires_at
    if expires is None:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires <= datetime.now(UTC)


def enqueue_archive_copy(session: Session, run_id: int, rows: Sequence[InventoryRow]) -> int:
    """Enqueue ready originals once; reset recoverable failures for a resume."""
    run = session.get(PipelineRun, run_id)
    if run is None or run.pipeline_type != "data_rebuild":
        raise ValueError("REBUILD_RUN_NOT_FOUND")
    added = 0
    for row in rows:
        if row.status != "ready":
            continue
        key = f"rebuild:{run_id}:archive:{row.file_id}:{row.sha256}"
        task = session.scalar(select(PipelineTask).where(PipelineTask.idempotency_key == key))
        output = _success_output(session, run_id, row)
        if output is not None:
            if task is None:
                task = PipelineTask(
                    run_id=run_id, queue_name=QUEUE_NAME, priority=100,
                    logical_item_key=f"rebuild-file:{row.file_id}", stage=ARCHIVE_STAGE,
                    status="completed", idempotency_key=key,
                    payload_json=json.dumps({"file_id": row.file_id, "sha256": row.sha256}, sort_keys=True),
                )
                session.add(task)
                session.flush()
            _mark_task_completed(session, task)
            continue
        if task is None:
            task = PipelineTask(
                run_id=run_id, queue_name=QUEUE_NAME, priority=100,
                logical_item_key=f"rebuild-file:{row.file_id}", stage=ARCHIVE_STAGE,
                status="queued", idempotency_key=key,
                payload_json=json.dumps({"file_id": row.file_id, "sha256": row.sha256}, sort_keys=True),
            )
            session.add(task)
            session.flush()
            _add_event(session, task, "enqueued", "queued")
            added += 1
        elif task.status == "failed" and task.recoverable:
            task.status = "queued"
            task.error_code = task.last_error = None
            task.finished_at = None
            _add_event(
                session, task,
                "requeued", "queued",
            )
            added += 1
        elif (
            task.status == "running"
            and task.recoverable
            and _lease_expired(task)
        ):
            task.status = "queued"
            task.error_code = task.last_error = None
            task.finished_at = None
            task.lease_owner = task.lease_expires_at = None
            _add_event(session, task, "recovered", "queued")
            added += 1
    if added:
        run.status = "running"
        run.finished_at = None
    session.commit()
    _finish_run(session, run)
    return added


def _complete_inventory_task(session: Session, run_id: int, total: int) -> None:
    key = f"rebuild:{run_id}:inventory"
    task = session.scalar(select(PipelineTask).where(PipelineTask.idempotency_key == key))
    if task is None:
        task = PipelineTask(
            run_id=run_id, queue_name=QUEUE_NAME, priority=100,
            logical_item_key=f"rebuild-run:{run_id}", stage=INVENTORY_STAGE,
            status="completed", idempotency_key=key, progress_current=total,
            progress_total=total, finished_at=datetime.now(UTC),
        )
        session.add(task)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            return
        _add_event(session, task, "completed", "completed")
        session.commit()


def _claim_task(session: Session, task_id: int) -> PipelineTask | None:
    """Atomically move one queued task to running before invoking the copier."""
    owner = f"rebuild-cli:{uuid4().hex}"
    claimed = session.execute(update(PipelineTask).where(
        PipelineTask.id == task_id,
        PipelineTask.status == "queued",
    ).values(
        status="running", lease_owner=owner, started_at=datetime.now(UTC),
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        attempts=PipelineTask.attempts + 1,
    )).rowcount
    if claimed != 1:
        session.rollback()
        return None
    task = session.get(PipelineTask, task_id)
    assert task is not None
    _add_event(session, task, "claimed", "running")
    session.commit()
    return task


def _finish_run(session: Session, run: PipelineRun) -> None:
    tasks = session.scalars(select(PipelineTask).where(PipelineTask.run_id == run.id)).all()
    run.total_tasks = len(tasks)
    run.completed_tasks = sum(task.status == "completed" for task in tasks)
    run.failed_tasks = sum(task.status == "failed" for task in tasks)
    if all(task.status in {"completed", "failed"} for task in tasks):
        run.status = "completed" if run.failed_tasks == 0 else "failed"
        run.finished_at = datetime.now(UTC)
    session.commit()


def execute_archive_copy(
    session: Session, settings: Settings, run_id: int, rows: Sequence[InventoryRow]
) -> dict[str, int]:
    """Synchronously drive this run's archive tasks to terminal states."""
    run = session.get(PipelineRun, run_id)
    if run is None or run.pipeline_type != "data_rebuild":
        raise ValueError("REBUILD_RUN_NOT_FOUND")
    by_file_id = {row.file_id: row for row in rows if row.status == "ready"}
    _complete_inventory_task(session, run_id, len(rows))
    for row in by_file_id.values():
        key = f"rebuild:{run_id}:archive:{row.file_id}:{row.sha256}"
        task = session.scalar(select(PipelineTask).where(PipelineTask.idempotency_key == key))
        if task is not None and _success_output(session, run_id, row) is not None:
            _mark_task_completed(session, task)
    session.commit()
    tasks = session.scalars(select(PipelineTask).where(
        PipelineTask.run_id == run_id, PipelineTask.queue_name == QUEUE_NAME,
        PipelineTask.stage == ARCHIVE_STAGE, PipelineTask.status == "queued",
    ).order_by(PipelineTask.id)).all()
    copied = failed = 0
    for queued_task in tasks:
        task = _claim_task(session, queued_task.id)
        if task is None:
            continue
        payload = json.loads(task.payload_json)
        row = by_file_id.get(payload.get("file_id"))
        if row is None or row.sha256 != payload.get("sha256"):
            task.status, task.error_code, task.recoverable = "failed", "INVENTORY_CHANGED", True
            task.finished_at = datetime.now(UTC)
            _add_event(session, task, "failed", "failed", error_code=task.error_code)
            session.commit()
            failed += 1
            continue
        try:
            output = copy_inventory_row(session, settings, row, run_id=run_id)
        except Exception:  # noqa: BLE001 - record a sanitized task failure for any copier crash.
            task.status, task.error_code, task.recoverable = "failed", "COPY_EXCEPTION", True
            task.finished_at = datetime.now(UTC)
            _add_event(session, task, "failed", "failed", error_code=task.error_code)
            session.commit()
            failed += 1
            continue
        if output.status == "success":
            _mark_task_completed(session, task)
            task.recoverable = True
            copied += 1
        else:
            task.status, task.error_code, task.recoverable = "failed", output.error_code, True
            task.finished_at = datetime.now(UTC)
            _add_event(session, task, "failed", "failed", error_code=output.error_code)
            failed += 1
        session.commit()
    _finish_run(session, run)
    return {"copied": copied, "failed": failed}
