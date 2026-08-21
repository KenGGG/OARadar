"""Synthetic orchestration tests for the local clean-archive campaign."""

from __future__ import annotations

import hashlib
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import (
    ArchivedFile,
    OAItem,
    PipelineEvent,
    PipelineTask,
    RebuildOutput,
)
from oa_knowledge.rebuild import campaign
from oa_knowledge.rebuild.campaign import (
    create_rebuild_run,
    enqueue_archive_copy,
    execute_archive_copy,
)
from oa_knowledge.rebuild.inventory import build_inventory


@pytest.fixture
def settings(tmp_path):
    value = Settings(
        app={"data_root": tmp_path / "live-data"},
        rebuild={"target_root": tmp_path / "clean-rebuild"},
    )
    value.data_root.mkdir(parents=True)
    return value


@pytest.fixture
def session(settings):
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as value:
        yield value


@pytest.fixture
def inventory_row(session, settings):
    content = b"synthetic campaign original"
    relpath = "archive/raw/oa/done/synthetic/campaign.bin"
    source = settings.data_root / relpath
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    item = OAItem(
        oa_item_key="done:campaign", source_channel="done", title="Synthetic",
        initiated_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    session.add(item)
    session.flush()
    file = ArchivedFile(
        oa_item_id=item.id, original_name="campaign.bin", attachment_key="campaign",
        file_role="direct_attachment", source_container_key="root", local_relpath=relpath,
        size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest(),
        download_status="verified",
    )
    session.add(file)
    session.commit()
    return build_inventory(session, settings)[0]


def test_enqueue_uses_required_idempotency_key_and_skips_successful_output(
    session, settings, inventory_row
) -> None:
    run = create_rebuild_run(session, cutoff_at=datetime(2026, 8, 21, tzinfo=UTC))
    assert enqueue_archive_copy(session, run.id, [inventory_row]) == 1
    task = session.scalar(select(PipelineTask).where(
        PipelineTask.run_id == run.id, PipelineTask.stage == "archive_copy"
    ))
    assert task.idempotency_key == f"rebuild:{run.id}:archive:{inventory_row.file_id}:{inventory_row.sha256}"

    execute_archive_copy(session, settings, run.id, [inventory_row])
    assert session.scalar(select(RebuildOutput)).status == "success"
    assert enqueue_archive_copy(session, run.id, [inventory_row]) == 0


def test_execute_drives_enqueued_task_and_run_to_terminal_outcomes(
    session, settings, inventory_row
) -> None:
    run = create_rebuild_run(session, cutoff_at=datetime(2026, 8, 21, tzinfo=UTC))
    enqueue_archive_copy(session, run.id, [inventory_row])

    result = execute_archive_copy(session, settings, run.id, [inventory_row])

    task = session.scalar(select(PipelineTask).where(
        PipelineTask.run_id == run.id, PipelineTask.stage == "archive_copy"
    ))
    session.refresh(run)
    assert result == {"copied": 1, "failed": 0}
    assert task.status == "completed"
    assert run.status == "completed"
    assert run.completed_tasks == 2


def test_create_rebuild_run_requires_an_aware_cutoff(session) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        create_rebuild_run(session, cutoff_at=datetime(2026, 8, 21))  # noqa: DTZ001


def test_resume_recovers_interrupted_archive_task(session, inventory_row) -> None:
    run = create_rebuild_run(session, cutoff_at=datetime(2026, 8, 21, tzinfo=UTC))
    enqueue_archive_copy(session, run.id, [inventory_row])
    task = session.scalar(select(PipelineTask).where(PipelineTask.run_id == run.id))
    task.status = "running"
    session.commit()

    assert enqueue_archive_copy(session, run.id, [inventory_row]) == 1
    assert task.status == "queued"


def test_resume_recovers_expired_naive_sqlite_lease(session, inventory_row) -> None:
    run = create_rebuild_run(session, cutoff_at=datetime(2026, 8, 21, tzinfo=UTC))
    enqueue_archive_copy(session, run.id, [inventory_row])
    task = session.scalar(select(PipelineTask).where(PipelineTask.run_id == run.id))
    task.status = "running"
    task.lease_expires_at = datetime(2020, 1, 1)  # noqa: DTZ001 - SQLite legacy value.
    session.commit()

    assert enqueue_archive_copy(session, run.id, [inventory_row]) == 1
    assert task.status == "queued"


def test_resume_reconciles_successful_output_after_task_completion_crash(
    session, settings, inventory_row
) -> None:
    run = create_rebuild_run(session, cutoff_at=datetime(2026, 8, 21, tzinfo=UTC))
    enqueue_archive_copy(session, run.id, [inventory_row])
    task = session.scalar(select(PipelineTask).where(PipelineTask.run_id == run.id))
    task.status = "running"
    session.commit()

    campaign.copy_inventory_row(session, settings, inventory_row, run_id=run.id)

    assert enqueue_archive_copy(session, run.id, [inventory_row]) == 0
    session.refresh(task)
    assert task.status == "completed"
    assert session.scalars(select(PipelineEvent).where(
        PipelineEvent.task_id == task.id,
        PipelineEvent.event_type == "completed",
    )).all()
    execute_archive_copy(session, settings, run.id, [inventory_row])
    session.refresh(run)
    assert run.status == "completed"


def test_concurrent_execute_claims_and_completes_task_once(
    session, settings, inventory_row, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = create_rebuild_run(session, cutoff_at=datetime(2026, 8, 21, tzinfo=UTC))
    enqueue_archive_copy(session, run.id, [inventory_row])
    run_id = run.id
    calls = 0
    call_lock = threading.Lock()

    def copy_once(*args, **kwargs):
        nonlocal calls
        with call_lock:
            calls += 1
        time.sleep(0.05)
        return SimpleNamespace(status="success", error_code=None)

    monkeypatch.setattr(campaign, "copy_inventory_row", copy_once)
    engine = session.get_bind()
    barrier = threading.Barrier(2)

    def execute() -> None:
        with Session(engine) as concurrent_session:
            barrier.wait()
            execute_archive_copy(concurrent_session, settings, run_id, [inventory_row])

    first = threading.Thread(target=execute)
    second = threading.Thread(target=execute)
    first.start(); second.start(); first.join(); second.join()

    task = session.scalar(select(PipelineTask).where(PipelineTask.run_id == run.id))
    events = session.scalars(select(PipelineEvent).where(PipelineEvent.task_id == task.id)).all()
    assert calls == 1
    assert [event.event_type for event in events].count("claimed") == 1
    assert [event.event_type for event in events].count("completed") == 1


def test_heartbeat_keeps_blocking_copy_owned_during_concurrent_resume(
    session, settings, inventory_row, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A still-running local copier must not be reclaimed after a short lease."""
    run = create_rebuild_run(session, cutoff_at=datetime(2026, 8, 21, tzinfo=UTC))
    enqueue_archive_copy(session, run.id, [inventory_row])
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    monkeypatch.setattr(campaign, "LEASE_TTL", timedelta(milliseconds=40))
    monkeypatch.setattr(campaign, "LEASE_HEARTBEAT_INTERVAL", 0.01)

    def blocking_copy(*args, **kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        entered.set()
        assert release.wait(2)
        return SimpleNamespace(status="success", error_code=None)

    monkeypatch.setattr(campaign, "copy_inventory_row", blocking_copy)
    engine = session.get_bind()

    def execute() -> None:
        with Session(engine) as worker_session:
            execute_archive_copy(worker_session, settings, run.id, [inventory_row])

    worker = threading.Thread(target=execute)
    worker.start()
    assert entered.wait(2)
    time.sleep(0.12)
    with Session(engine) as resume_session:
        assert enqueue_archive_copy(resume_session, run.id, [inventory_row]) == 0
        execute_archive_copy(resume_session, settings, run.id, [inventory_row])
    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()

    task = session.scalar(select(PipelineTask).where(PipelineTask.run_id == run.id))
    events = session.scalars(select(PipelineEvent).where(PipelineEvent.task_id == task.id)).all()
    assert calls == 1
    assert [event.event_type for event in events].count("claimed") == 1
    assert [event.event_type for event in events].count("completed") == 1


def test_stale_owner_cannot_complete_task_or_emit_terminal_event(session, inventory_row) -> None:
    run = create_rebuild_run(session, cutoff_at=datetime(2026, 8, 21, tzinfo=UTC))
    enqueue_archive_copy(session, run.id, [inventory_row])
    task = session.scalar(select(PipelineTask).where(PipelineTask.run_id == run.id))
    task.status = "running"
    task.lease_owner = "new-owner"
    session.commit()

    assert campaign._complete_owned_task(session, task.id, "stale-owner") is False
    session.refresh(task)
    assert task.status == "running"
    assert session.scalars(select(PipelineEvent).where(
        PipelineEvent.task_id == task.id,
        PipelineEvent.event_type == "completed",
    )).all() == []


def test_heartbeat_stops_when_copy_is_interrupted(
    session, settings, inventory_row, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = create_rebuild_run(session, cutoff_at=datetime(2026, 8, 21, tzinfo=UTC))
    enqueue_archive_copy(session, run.id, [inventory_row])
    monkeypatch.setattr(campaign, "LEASE_TTL", timedelta(milliseconds=40))
    monkeypatch.setattr(campaign, "LEASE_HEARTBEAT_INTERVAL", 0.01)

    def interrupted_copy(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(campaign, "copy_inventory_row", interrupted_copy)
    with pytest.raises(KeyboardInterrupt):
        execute_archive_copy(session, settings, run.id, [inventory_row])
    time.sleep(0.12)
    assert enqueue_archive_copy(session, run.id, [inventory_row]) == 1


def test_concurrent_first_enqueue_converges_on_one_task(session, inventory_row) -> None:
    run = create_rebuild_run(session, cutoff_at=datetime(2026, 8, 21, tzinfo=UTC))
    engine = session.get_bind()
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def enqueue() -> None:
        try:
            with Session(engine) as concurrent_session:
                barrier.wait()
                enqueue_archive_copy(concurrent_session, run.id, [inventory_row])
        except Exception as exc:  # noqa: BLE001 - assertion captures any race leak.
            errors.append(exc)

    first = threading.Thread(target=enqueue)
    second = threading.Thread(target=enqueue)
    first.start(); second.start(); first.join(); second.join()

    tasks = session.scalars(select(PipelineTask).where(
        PipelineTask.run_id == run.id,
        PipelineTask.stage == "archive_copy",
    )).all()
    assert errors == []
    assert len(tasks) == 1
    events = session.scalars(select(PipelineEvent).where(PipelineEvent.task_id == tasks[0].id)).all()
    assert [event.event_type for event in events].count("enqueued") == 1


def test_concurrent_success_reconciliation_emits_one_completed_event(
    session, settings, inventory_row
) -> None:
    run = create_rebuild_run(session, cutoff_at=datetime(2026, 8, 21, tzinfo=UTC))
    enqueue_archive_copy(session, run.id, [inventory_row])
    task = session.scalar(select(PipelineTask).where(PipelineTask.run_id == run.id))
    task.status = "running"
    session.commit()
    campaign.copy_inventory_row(session, settings, inventory_row, run_id=run.id)

    engine = session.get_bind()
    barrier = threading.Barrier(2)

    def reconcile() -> None:
        with Session(engine) as concurrent_session:
            barrier.wait()
            enqueue_archive_copy(concurrent_session, run.id, [inventory_row])

    first = threading.Thread(target=reconcile)
    second = threading.Thread(target=reconcile)
    first.start(); second.start(); first.join(); second.join()

    session.refresh(task)
    events = session.scalars(select(PipelineEvent).where(PipelineEvent.task_id == task.id)).all()
    assert task.status == "completed"
    assert [event.event_type for event in events].count("completed") == 1
