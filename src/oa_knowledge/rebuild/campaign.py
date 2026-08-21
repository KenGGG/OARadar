"""Small, synchronous local orchestration for clean archive rebuilds."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.models import (
    ArchivedFile,
    OAItem,
    PipelineEvent,
    PipelineRun,
    PipelineTask,
    RebuildOutput,
)
from oa_knowledge.rebuild.archive_copy import (
    CopyCancelled,
    copy_inventory_row,
    reconcile_successful_output,
)
from oa_knowledge.rebuild.body_source import body_markdown_filename
from oa_knowledge.rebuild.inventory import InventoryRow
from oa_knowledge.rebuild.paths import effective_item_date
from oa_knowledge.source_roles import MARKDOWN_SOURCE_ROLES

QUEUE_NAME = "data_rebuild"
ARCHIVE_STAGE = "archive_copy"
INVENTORY_STAGE = "inventory"
REBUILD_PARSE_STAGE = "rebuild_parse"
REBUILD_PUBLISH_STAGE = "rebuild_publish"
REBUILD_INDEX_STAGE = "rebuild_index"
MARKDOWN_REBUILD_STAGES = frozenset({
    REBUILD_PARSE_STAGE, REBUILD_PUBLISH_STAGE, REBUILD_INDEX_STAGE,
})
MARKDOWN_REBUILD_SAFETY_GATE = "MARKDOWN_REBUILD_PHASE4_CAS_REQUIRED"
RESUMABLE_RUN_STATUSES = frozenset({"running", "failed"})
BLOCKING_INVENTORY_STATUSES = (
    "depth_limit_reached", "hash_mismatch", "missing", "unsafe_path",
)
LEASE_TTL = timedelta(minutes=5)
LEASE_HEARTBEAT_INTERVAL = 60.0
LEASE_HEARTBEAT_RETRY_INITIAL = 1.0
LEASE_HEARTBEAT_RETRY_MAX = 15.0


@dataclass
class _LeaseHealth:
    """Shared, in-process fence for one synchronous copy claim."""

    lost: threading.Event = field(default_factory=threading.Event)

    def is_healthy(self) -> bool:
        return not self.lost.is_set()


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


def _add_event(
    session: Session,
    task: PipelineTask,
    event_type: str,
    status: str,
    *,
    error_code: str | None = None,
    details: dict[str, int] | None = None,
) -> None:
    safe_details = details or ({} if error_code is None else {"error_code": error_code})
    session.add(PipelineEvent(
        task_id=task.id, event_type=event_type, stage=task.stage, status=status,
        details_json=json.dumps(safe_details, sort_keys=True),
    ))


def resume_rebuild_run(session: Session, run_id: int) -> PipelineRun:
    """Return only an unfinished/recoverable local rebuild run for CLI resume."""
    run = session.get(PipelineRun, run_id)
    if run is None or run.pipeline_type != "data_rebuild":
        raise ValueError("REBUILD_RUN_NOT_FOUND")
    if run.status not in RESUMABLE_RUN_STATUSES:
        raise ValueError("REBUILD_RUN_NOT_RESUMABLE")
    return run


def inventory_blocker_counts(summary: dict[str, int]) -> dict[str, int]:
    """Return the fixed, safe blocker-count contract used by CLI and events."""
    return {status: summary.get(status, 0) for status in BLOCKING_INVENTORY_STATUSES}


def block_rebuild_run(
    session: Session, run_id: int, summary: dict[str, int],
) -> dict[str, int]:
    """Persist a terminal inventory gate without enqueueing any copy work."""
    run = session.get(PipelineRun, run_id)
    if run is None or run.pipeline_type != "data_rebuild":
        raise ValueError("REBUILD_RUN_NOT_FOUND")
    blockers = inventory_blocker_counts(summary)
    if not any(blockers.values()):
        raise ValueError("INVENTORY_NOT_BLOCKED")
    key = f"rebuild:{run_id}:inventory"
    task = session.scalar(select(PipelineTask).where(PipelineTask.idempotency_key == key))
    if task is None:
        task = PipelineTask(
            run_id=run_id,
            queue_name=QUEUE_NAME,
            priority=100,
            logical_item_key=f"rebuild-run:{run_id}",
            stage=INVENTORY_STAGE,
            status="failed",
            idempotency_key=key,
            progress_current=summary.get("ready", 0),
            progress_total=summary.get("total", 0),
            error_code="INVENTORY_BLOCKED",
            recoverable=True,
            finished_at=datetime.now(UTC),
            payload_json=json.dumps(blockers, sort_keys=True),
        )
        session.add(task)
        session.flush()
    else:
        task.status = "failed"
        task.progress_current = summary.get("ready", 0)
        task.progress_total = summary.get("total", 0)
        task.error_code = "INVENTORY_BLOCKED"
        task.recoverable = True
        task.finished_at = datetime.now(UTC)
        task.payload_json = json.dumps(blockers, sort_keys=True)
    _add_event(session, task, "blocked", "failed", details=blockers)
    session.commit()
    _finish_run(session, run_id)
    return blockers


def rebuild_status_summary(session: Session) -> dict[str, object]:
    """Return safe counts and discoverable IDs for the latest rebuild state."""
    runs = session.scalar(select(func.count()).select_from(PipelineRun).where(
        PipelineRun.pipeline_type == "data_rebuild"
    )) or 0
    latest = session.scalar(select(PipelineRun).where(
        PipelineRun.pipeline_type == "data_rebuild"
    ).order_by(PipelineRun.id.desc()))
    resumable = session.scalar(select(PipelineRun).where(
        PipelineRun.pipeline_type == "data_rebuild",
        PipelineRun.status.in_(RESUMABLE_RUN_STATUSES),
    ).order_by(PipelineRun.id.desc()))
    empty = {status: 0 for status in ("queued", "running", "completed", "failed")}
    if latest is None:
        return {
            "runs": runs,
            "latest_run_id": None,
            "resumable_run_id": None,
            "latest_started_at": None,
            "latest_finished_at": None,
            **empty,
            "blockers": inventory_blocker_counts({}),
            "execution_allowed": False,
            "safety_gate": MARKDOWN_REBUILD_SAFETY_GATE,
            "error_codes": [],
        }
    tasks = session.scalars(select(PipelineTask).where(PipelineTask.run_id == latest.id)).all()
    counts = {
        status: sum(task.status == status for task in tasks)
        for status in ("queued", "running", "completed", "failed")
    }
    inventory_task = next((task for task in tasks if task.stage == INVENTORY_STAGE), None)
    blockers = inventory_blocker_counts({})
    if inventory_task is not None and inventory_task.error_code == "INVENTORY_BLOCKED":
        try:
            blockers = inventory_blocker_counts(json.loads(inventory_task.payload_json))
        except (TypeError, ValueError):
            pass
    return {
        "runs": runs,
        "latest_run_id": latest.id,
        "resumable_run_id": resumable.id if resumable is not None else None,
        "latest_started_at": latest.started_at.isoformat() if latest.started_at else None,
        "latest_finished_at": latest.finished_at.isoformat() if latest.finished_at else None,
        **counts,
        "blockers": blockers,
        "execution_allowed": False,
        "safety_gate": MARKDOWN_REBUILD_SAFETY_GATE,
        "error_codes": sorted({
            task.error_code for task in tasks if task.error_code
        }),
    }


def _eligible_markdown_rebuild_item(item: OAItem) -> bool:
    """Admission is metadata-only; worker stages revalidate copied evidence."""
    if item.source_channel != "done" or item.classification_state != "confirmed":
        return False
    try:
        effective_item_date(item)
    except ValueError:
        return False
    return True


def _markdown_task_key(run_id: int, stage: str, target_id: int, source_sha256: str) -> str:
    return f"rebuild:{run_id}:{stage}:{target_id}:{source_sha256}"


def _enqueue_markdown_task(
    session: Session,
    *,
    run_id: int,
    item_id: int,
    stage: str,
    target_id: int,
    source_sha256: str,
    payload: dict[str, object],
) -> bool:
    """Create one local rebuild task, preserving idempotent retry semantics."""
    key = _markdown_task_key(run_id, stage, target_id, source_sha256)
    task = session.scalar(select(PipelineTask).where(PipelineTask.idempotency_key == key))
    if task is None:
        task = PipelineTask(
            run_id=run_id,
            queue_name=QUEUE_NAME,
            priority=100,
            logical_item_key=f"rebuild-item:{item_id}",
            stage=stage,
            status="queued",
            idempotency_key=key,
            payload_json=json.dumps(payload, sort_keys=True),
        )
        session.add(task)
        session.flush()
        _add_event(session, task, "enqueued", "queued")
        return True
    if task.status == "failed" and task.recoverable:
        task.status = "queued"
        task.error_code = task.last_error = None
        task.finished_at = None
        _add_event(session, task, "requeued", "queued")
        return True
    return False


def _current_original_fingerprint(session: Session, *, run_id: int, item_id: int) -> str:
    """Fingerprint only current-run copied originals bound to current source hashes."""
    pairs = session.execute(
        select(ArchivedFile.id, ArchivedFile.sha256)
        .join(RebuildOutput, RebuildOutput.source_file_id == ArchivedFile.id)
        .where(
            RebuildOutput.run_id == run_id,
            RebuildOutput.oa_item_id == item_id,
            RebuildOutput.kind == "original",
            RebuildOutput.status == "success",
            ArchivedFile.oa_item_id == item_id,
            ArchivedFile.download_status == "verified",
            ArchivedFile.sha256.is_not(None),
            RebuildOutput.sha256 == ArchivedFile.sha256,
        )
        .order_by(ArchivedFile.id, RebuildOutput.id.desc())
    ).all()
    values: dict[int, str] = {}
    for source_id, source_sha in pairs:
        values.setdefault(source_id, source_sha)
    evidence = "\0".join(f"{source_id}:{source_sha}" for source_id, source_sha in values.items())
    return hashlib.sha256(evidence.encode()).hexdigest()


def _current_page_body_source(
    session: Session, *, run_id: int, item_id: int,
) -> ArchivedFile | None:
    """Return only a verified body snapshot with a matching copied-original row."""
    return session.scalar(
        select(ArchivedFile)
        .join(RebuildOutput, RebuildOutput.source_file_id == ArchivedFile.id)
        .where(
            ArchivedFile.oa_item_id == item_id,
            ArchivedFile.file_role == "body_snapshot",
            ArchivedFile.download_status == "verified",
            ArchivedFile.sha256.is_not(None),
            RebuildOutput.run_id == run_id,
            RebuildOutput.oa_item_id == item_id,
            RebuildOutput.kind == "original",
            RebuildOutput.status == "success",
            RebuildOutput.sha256 == ArchivedFile.sha256,
        )
        .order_by(RebuildOutput.id.desc())
        .limit(1)
    )


def enqueue_markdown_rebuild(
    session: Session, run_id: int, item_ids: Sequence[int],
) -> int:
    """Queue rebuilt-source parsing for confirmed, dated items only.

    This deliberately creates only existing ``PipelineTask`` rows.  Each
    claimed stage repeats the local-ledger and protected-file verification, so
    enqueue-time database state alone never authorizes publication.
    """
    run = session.get(PipelineRun, run_id)
    if run is None or run.pipeline_type != "data_rebuild":
        raise ValueError("REBUILD_RUN_NOT_FOUND")
    added = 0
    for item_id in dict.fromkeys(item_ids):
        item = session.get(OAItem, item_id)
        if item is None or not _eligible_markdown_rebuild_item(item):
            continue
        files = list(session.scalars(select(ArchivedFile).where(
            ArchivedFile.oa_item_id == item.id,
            ArchivedFile.file_role.in_(MARKDOWN_SOURCE_ROLES),
            ArchivedFile.download_status == "verified",
            ArchivedFile.sha256.is_not(None),
        ).order_by(ArchivedFile.id)))
        for source in files:
            copied = session.scalar(select(RebuildOutput).where(
                RebuildOutput.run_id == run_id,
                RebuildOutput.oa_item_id == item.id,
                RebuildOutput.source_file_id == source.id,
                RebuildOutput.kind == "original",
                RebuildOutput.status == "success",
                RebuildOutput.sha256 == source.sha256,
            ).order_by(RebuildOutput.id.desc()).limit(1))
            if copied is None:
                continue
            source_sha = source.sha256
            assert source_sha is not None
            if _enqueue_markdown_task(
                session,
                run_id=run_id,
                item_id=item.id,
                stage=REBUILD_PARSE_STAGE,
                target_id=source.id,
                source_sha256=source_sha,
                payload={
                    "item_id": item.id,
                    "source_file_id": source.id,
                    "source_sha256": source_sha,
                },
            ):
                added += 1
        if files:
            continue
        page = _current_page_body_source(session, run_id=run_id, item_id=item.id)
        if (
            page is not None
            and page.sha256
            and body_markdown_filename(item) is not None
            and _enqueue_markdown_task(
                session,
                run_id=run_id,
                item_id=item.id,
                stage=REBUILD_PUBLISH_STAGE,
                target_id=item.id,
                source_sha256=page.sha256,
                payload={
                    "item_id": item.id,
                    "kind": "body",
                    "source_file_id": page.id,
                    "source_sha256": page.sha256,
                },
            )
        ):
            added += 1
        # A confirmed item with no Markdown-convertible source still has a
        # useful metadata/original-evidence index.  Page body publication (if
        # present) is inserted first so this index cannot run ahead of it.
        fingerprint = _current_original_fingerprint(session, run_id=run_id, item_id=item.id)
        if _enqueue_markdown_task(
            session,
            run_id=run_id,
            item_id=item.id,
            stage=REBUILD_INDEX_STAGE,
            target_id=item.id,
            source_sha256=fingerprint,
            payload={"item_id": item.id, "source_sha256": fingerprint},
        ):
            added += 1
    if added:
        run.status = "running"
        run.finished_at = None
    session.commit()
    _finish_run(session, run_id)
    return added


def _reconcile_successful_task(session: Session, task_id: int) -> bool:
    """Atomically turn a durable output into one completed task/event pair."""
    completed = session.execute(update(PipelineTask).where(
        PipelineTask.id == task_id,
        PipelineTask.status != "completed",
    ).values(
        status="completed", error_code=None, last_error=None,
        finished_at=datetime.now(UTC), lease_owner=None, lease_expires_at=None,
    )).rowcount
    if completed != 1:
        session.rollback()
        return False
    task = session.get(PipelineTask, task_id)
    assert task is not None
    _add_event(session, task, "completed", "completed")
    session.commit()
    return True


def _success_output(
    session: Session, settings: Settings, run_id: int, row: InventoryRow,
) -> RebuildOutput | None:
    return reconcile_successful_output(session, settings, row, run_id=run_id)


def _lease_expired(task: PipelineTask) -> bool:
    expires = task.lease_expires_at
    if expires is None:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires <= datetime.now(UTC)


def _new_archive_task(run_id: int, row: InventoryRow, key: str) -> PipelineTask:
    return PipelineTask(
        run_id=run_id, queue_name=QUEUE_NAME, priority=100,
        logical_item_key=f"rebuild-file:{row.file_id}", stage=ARCHIVE_STAGE,
        status="queued", idempotency_key=key,
        payload_json=json.dumps({"file_id": row.file_id, "sha256": row.sha256}, sort_keys=True),
    )


def _recover_expired_task(
    session: Session,
    task_id: int,
    *,
    observed_status: str,
    observed_owner: str | None,
    observed_expiry: datetime | None,
) -> bool:
    """Recover only the exact expired lease snapshot previously observed."""
    recovered = session.execute(update(PipelineTask).where(
        PipelineTask.id == task_id,
        PipelineTask.status == observed_status,
        PipelineTask.lease_owner == observed_owner,
        PipelineTask.lease_expires_at == observed_expiry,
    ).values(
        status="queued",
        error_code=None,
        last_error=None,
        finished_at=None,
        lease_owner=None,
        lease_expires_at=None,
    )).rowcount
    if recovered != 1:
        session.expire_all()
        return False
    session.expire_all()
    task = session.get(PipelineTask, task_id)
    assert task is not None
    _add_event(session, task, "recovered", "queued")
    session.commit()
    return True


def _get_or_create_archive_task(
    session: Session, run_id: int, row: InventoryRow, key: str,
) -> tuple[PipelineTask, bool]:
    """Insert under a savepoint so concurrent first enqueues converge safely."""
    task = session.scalar(select(PipelineTask).where(PipelineTask.idempotency_key == key))
    if task is not None:
        return task, False
    task = _new_archive_task(run_id, row, key)
    try:
        with session.begin_nested():
            session.add(task)
            session.flush()
    except IntegrityError:
        task = session.scalar(select(PipelineTask).where(PipelineTask.idempotency_key == key))
        if task is None:
            raise
        return task, False
    return task, True


def enqueue_archive_copy(
    session: Session,
    run_id: int,
    rows: Sequence[InventoryRow],
    settings: Settings | None = None,
) -> int:
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
        # Without settings there is no protected-path proof, so defer any
        # success reconciliation to execute_archive_copy instead of trusting
        # ledger columns alone.
        output = (
            _success_output(session, settings, run_id, row)
            if settings is not None
            else None
        )
        if output is not None:
            if task is None:
                task, _ = _get_or_create_archive_task(session, run_id, row, key)
            session.commit()
            _reconcile_successful_task(session, task.id)
            continue
        if task is None:
            task, created = _get_or_create_archive_task(session, run_id, row, key)
            if created:
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
            if _recover_expired_task(
                session,
                task.id,
                observed_status=task.status,
                observed_owner=task.lease_owner,
                observed_expiry=task.lease_expires_at,
            ):
                added += 1
    if added:
        session.execute(update(PipelineRun).where(PipelineRun.id == run_id).values(
            status="running", finished_at=None,
        ))
    session.commit()
    _finish_run(session, run_id)
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
    elif task.status != "completed":
        task.status = "completed"
        task.error_code = task.last_error = None
        task.progress_current = task.progress_total = total
        task.finished_at = datetime.now(UTC)
        task.payload_json = "{}"
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
        lease_expires_at=datetime.now(UTC) + LEASE_TTL,
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


def _renew_lease(session: Session, *, task_id: int, owner: str) -> bool:
    """Attempt one owner-conditional renewal using the heartbeat session."""
    renewed = session.execute(update(PipelineTask).where(
        PipelineTask.id == task_id,
        PipelineTask.status == "running",
        PipelineTask.lease_owner == owner,
    ).values(lease_expires_at=datetime.now(UTC) + LEASE_TTL)).rowcount
    session.commit()
    return renewed == 1


def _heartbeat_retry_guard() -> float:
    return max(LEASE_HEARTBEAT_INTERVAL * 2, LEASE_TTL.total_seconds() / 4)


def _start_lease_heartbeat(
    session: Session, task: PipelineTask,
) -> tuple[threading.Event, threading.Thread, _LeaseHealth]:
    """Renew a claimed task while its synchronous local copier is active."""
    assert task.lease_owner is not None
    stop = threading.Event()
    health = _LeaseHealth()
    bind = session.get_bind()
    task_id, owner = task.id, task.lease_owner

    def renew() -> None:
        delay = LEASE_HEARTBEAT_INTERVAL
        retry_delay = LEASE_HEARTBEAT_RETRY_INITIAL
        deadline = datetime.now(UTC) + LEASE_TTL
        while not stop.wait(delay):
            try:
                with Session(bind=bind) as heartbeat_session:
                    renewed = _renew_lease(heartbeat_session, task_id=task_id, owner=owner)
            except SQLAlchemyError:
                # A separate Session owns this transaction; closing it rolls back
                # any failed SQLite statement before the bounded retry.
                remaining = (deadline - datetime.now(UTC)).total_seconds()
                if remaining <= _heartbeat_retry_guard():
                    health.lost.set()
                    return
                delay = min(retry_delay, max(0.001, remaining - _heartbeat_retry_guard()))
                retry_delay = min(retry_delay * 2, LEASE_HEARTBEAT_RETRY_MAX)
                continue
            if renewed != 1:
                health.lost.set()
                return
            deadline = datetime.now(UTC) + LEASE_TTL
            delay = LEASE_HEARTBEAT_INTERVAL
            retry_delay = LEASE_HEARTBEAT_RETRY_INITIAL

    thread = threading.Thread(target=renew, name=f"rebuild-lease-{task_id}", daemon=True)
    thread.start()
    return stop, thread, health


def _stop_lease_heartbeat(stop: threading.Event, thread: threading.Thread) -> None:
    stop.set()
    thread.join()


def _complete_owned_task(session: Session, task_id: int, owner: str) -> bool:
    """Only the current claimant may write its successful terminal transition."""
    completed = session.execute(update(PipelineTask).where(
        PipelineTask.id == task_id,
        PipelineTask.status == "running",
        PipelineTask.lease_owner == owner,
    ).values(
        status="completed", error_code=None, last_error=None,
        finished_at=datetime.now(UTC), lease_owner=None, lease_expires_at=None,
    )).rowcount
    if completed != 1:
        session.rollback()
        return False
    task = session.get(PipelineTask, task_id)
    assert task is not None
    _add_event(session, task, "completed", "completed")
    session.commit()
    return True


def _fail_owned_task(session: Session, task_id: int, owner: str, error_code: str) -> bool:
    """Only the current claimant may write a failed terminal transition."""
    failed = session.execute(update(PipelineTask).where(
        PipelineTask.id == task_id,
        PipelineTask.status == "running",
        PipelineTask.lease_owner == owner,
    ).values(
        status="failed", error_code=error_code, last_error=None, recoverable=True,
        finished_at=datetime.now(UTC), lease_owner=None, lease_expires_at=None,
    )).rowcount
    if failed != 1:
        session.rollback()
        return False
    task = session.get(PipelineTask, task_id)
    assert task is not None
    _add_event(session, task, "failed", "failed", error_code=error_code)
    session.commit()
    return True


def _finish_run(session: Session, run_id: int) -> None:
    run = session.get(PipelineRun, run_id)
    if run is None:
        return
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
        if task is not None and _success_output(session, settings, run_id, row) is not None:
            _reconcile_successful_task(session, task.id)
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
        owner = task.lease_owner
        assert owner is not None
        payload = json.loads(task.payload_json)
        row = by_file_id.get(payload.get("file_id"))
        if row is None or row.sha256 != payload.get("sha256"):
            if _fail_owned_task(session, task.id, owner, "INVENTORY_CHANGED"):
                failed += 1
            continue
        heartbeat_stop, heartbeat_thread, lease_health = _start_lease_heartbeat(session, task)
        cancelled = False
        try:
            output = copy_inventory_row(
                session, settings, row, run_id=run_id,
                should_continue=lease_health.is_healthy,
            )
        except CopyCancelled:
            cancelled = True
            output = None
        except Exception:  # noqa: BLE001 - record a sanitized task failure for any copier crash.
            output = None
        finally:
            _stop_lease_heartbeat(heartbeat_stop, heartbeat_thread)
        if cancelled:
            continue
        if output is None:
            if _fail_owned_task(session, task.id, owner, "COPY_EXCEPTION"):
                failed += 1
        elif not lease_health.is_healthy():
            # The copier cooperatively fences real copies; this also fences a
            # late-returning implementation that cannot be interrupted mid-call.
            _fail_owned_task(session, task.id, owner, "LEASE_HEALTH_LOST")
        else:
            if output.status == "success":
                if _complete_owned_task(session, task.id, owner):
                    copied += 1
            elif _fail_owned_task(session, task.id, owner, output.error_code):
                failed += 1
    _finish_run(session, run.id)
    return {"copied": copied, "failed": failed}
