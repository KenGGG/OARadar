"""Small, synchronous local orchestration for clean archive rebuilds."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
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


def enqueue_archive_copy(session: Session, run_id: int, rows: Sequence[InventoryRow]) -> int:
    """Enqueue ready originals once; reset recoverable failures for a resume."""
    run = session.get(PipelineRun, run_id)
    if run is None or run.pipeline_type != "data_rebuild":
        raise ValueError("REBUILD_RUN_NOT_FOUND")
    added = 0
    for row in rows:
        if row.status != "ready":
            continue
        output = session.scalar(select(RebuildOutput).where(
            RebuildOutput.run_id == run_id,
            RebuildOutput.source_file_id == row.file_id,
            RebuildOutput.sha256 == row.sha256,
            RebuildOutput.status == "success",
        ))
        if output is not None:
            continue
        key = f"rebuild:{run_id}:archive:{row.file_id}:{row.sha256}"
        task = session.scalar(select(PipelineTask).where(PipelineTask.idempotency_key == key))
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
        elif task.status in {"failed", "running"} and task.recoverable:
            was_running = task.status == "running"
            task.status = "queued"
            task.error_code = task.last_error = None
            task.finished_at = None
            _add_event(
                session, task,
                "recovered" if was_running else "requeued", "queued",
            )
            added += 1
    if added:
        run.status = "running"
        run.finished_at = None
    session.commit()
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
        session.flush()
        _add_event(session, task, "completed", "completed")
        session.commit()


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
    tasks = session.scalars(select(PipelineTask).where(
        PipelineTask.run_id == run_id, PipelineTask.queue_name == QUEUE_NAME,
        PipelineTask.stage == ARCHIVE_STAGE, PipelineTask.status == "queued",
    ).order_by(PipelineTask.id)).all()
    copied = failed = 0
    for task in tasks:
        payload = json.loads(task.payload_json)
        row = by_file_id.get(payload.get("file_id"))
        task.status = "running"
        task.attempts += 1
        task.started_at = task.started_at or datetime.now(UTC)
        _add_event(session, task, "claimed", "running")
        session.commit()
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
            task.status, task.error_code, task.recoverable = "completed", None, True
            task.finished_at = datetime.now(UTC)
            _add_event(session, task, "completed", "completed")
            copied += 1
        else:
            task.status, task.error_code, task.recoverable = "failed", output.error_code, True
            task.finished_at = datetime.now(UTC)
            _add_event(session, task, "failed", "failed", error_code=output.error_code)
            failed += 1
        session.commit()
    _finish_run(session, run)
    return {"copied": copied, "failed": failed}
