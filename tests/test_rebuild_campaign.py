"""Synthetic orchestration tests for the local clean-archive campaign."""

from __future__ import annotations

import hashlib
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
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
from oa_knowledge.rebuild import archive_copy, campaign
from oa_knowledge.rebuild.campaign import (
    create_rebuild_run,
    enqueue_archive_copy,
    execute_archive_copy,
)
from oa_knowledge.rebuild.inventory import build_inventory
from oa_knowledge.rebuild.paths import resolve_rebuild_path


def _execute_in_session(engine, settings, run_id: int, inventory_row) -> None:
    with Session(engine) as worker_session:
        execute_archive_copy(worker_session, settings, run_id, [inventory_row])


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
    assert enqueue_archive_copy(session, run.id, [inventory_row], settings=settings) == 0


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

    assert enqueue_archive_copy(session, run.id, [inventory_row], settings=settings) == 0
    session.refresh(task)
    assert task.status == "completed"
    assert session.scalars(select(PipelineEvent).where(
        PipelineEvent.task_id == task.id,
        PipelineEvent.event_type == "completed",
    )).all()
    execute_archive_copy(session, settings, run.id, [inventory_row])
    session.refresh(run)
    assert run.status == "completed"


@pytest.mark.parametrize("damage", ("missing", "changed"))
def test_success_reconciliation_revalidates_and_repairs_or_conflicts(
    session, settings, inventory_row, damage: str
) -> None:
    run = create_rebuild_run(session, cutoff_at=datetime(2026, 8, 21, tzinfo=UTC))
    enqueue_archive_copy(session, run.id, [inventory_row])
    task = session.scalar(select(PipelineTask).where(PipelineTask.run_id == run.id))
    task.status = "running"
    session.commit()
    output = campaign.copy_inventory_row(session, settings, inventory_row, run_id=run.id)
    output_id = output.id
    target = resolve_rebuild_path(settings, output.target_relpath)
    if damage == "missing":
        target.unlink()
    else:
        target.write_bytes(b"changed synthetic final")

    assert enqueue_archive_copy(session, run.id, [inventory_row], settings=settings) == 1
    result = execute_archive_copy(session, settings, run.id, [inventory_row])

    session.refresh(task)
    output = session.get(RebuildOutput, output_id)
    if damage == "missing":
        assert result == {"copied": 1, "failed": 0}
        assert task.status == "completed"
        assert output.status == "success"
        assert target.read_bytes() == b"synthetic campaign original"
    else:
        assert result == {"copied": 0, "failed": 1}
        assert task.status == output.status == "failed"
        assert output.error_code == "TARGET_CONFLICT"
        assert target.read_bytes() == b"changed synthetic final"


def test_expired_recovery_cas_does_not_clear_a_renewed_lease(
    session, inventory_row
) -> None:
    run = create_rebuild_run(session, cutoff_at=datetime(2026, 8, 21, tzinfo=UTC))
    enqueue_archive_copy(session, run.id, [inventory_row])
    task = session.scalar(select(PipelineTask).where(PipelineTask.run_id == run.id))
    task.status = "running"
    task.lease_owner = "synthetic-owner"
    task.lease_expires_at = datetime(2020, 1, 1, tzinfo=UTC)
    session.commit()
    session.expire_all()
    observed = session.get(PipelineTask, task.id)
    observed_status = observed.status
    observed_owner = observed.lease_owner
    observed_expiry = observed.lease_expires_at

    engine = session.get_bind()
    with Session(engine) as renewal_session:
        renewed = renewal_session.get(PipelineTask, task.id)
        renewed.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        renewal_session.commit()

    recovered = campaign._recover_expired_task(
        session,
        task.id,
        observed_status=observed_status,
        observed_owner=observed_owner,
        observed_expiry=observed_expiry,
    )

    session.expire_all()
    current = session.get(PipelineTask, task.id)
    assert recovered is False
    assert current.status == "running"
    assert current.lease_owner == "synthetic-owner"
    assert current.lease_expires_at != observed_expiry
    assert session.scalars(select(PipelineEvent).where(
        PipelineEvent.task_id == task.id,
        PipelineEvent.event_type == "recovered",
    )).all() == []


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
            enqueue_archive_copy(
                concurrent_session, run.id, [inventory_row], settings=settings,
            )

    first = threading.Thread(target=reconcile)
    second = threading.Thread(target=reconcile)
    first.start(); second.start(); first.join(); second.join()

    session.refresh(task)
    events = session.scalars(select(PipelineEvent).where(PipelineEvent.task_id == task.id)).all()
    assert task.status == "completed"
    assert [event.event_type for event in events].count("completed") == 1


def test_transient_heartbeat_database_error_retries_without_reclaiming_copy(
    session, settings, inventory_row, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = create_rebuild_run(session, cutoff_at=datetime(2026, 8, 21, tzinfo=UTC))
    enqueue_archive_copy(session, run.id, [inventory_row])
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    original_renew = campaign._renew_lease
    failures = 0

    monkeypatch.setattr(campaign, "LEASE_TTL", timedelta(milliseconds=100))
    monkeypatch.setattr(campaign, "LEASE_HEARTBEAT_INTERVAL", 0.01)
    monkeypatch.setattr(campaign, "LEASE_HEARTBEAT_RETRY_INITIAL", 0.005)

    def transient_failure(*args, **kwargs):
        nonlocal failures
        failures += 1
        if failures == 1:
            raise OperationalError("update", {}, RuntimeError("busy"))
        return original_renew(*args, **kwargs)

    def blocking_copy(*args, **kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(2)
        return SimpleNamespace(status="success", error_code=None)

    monkeypatch.setattr(campaign, "_renew_lease", transient_failure)
    monkeypatch.setattr(campaign, "copy_inventory_row", blocking_copy)
    engine = session.get_bind()

    worker = threading.Thread(target=lambda: _execute_in_session(
        engine, settings, run.id, inventory_row,
    ))
    worker.start()
    assert entered.wait(2)
    time.sleep(0.15)
    with Session(engine) as resume_session:
        assert enqueue_archive_copy(resume_session, run.id, [inventory_row]) == 0
    release.set()
    worker.join(timeout=2)

    task = session.scalar(select(PipelineTask).where(PipelineTask.run_id == run.id))
    events = session.scalars(select(PipelineEvent).where(PipelineEvent.task_id == task.id)).all()
    assert failures >= 2
    assert calls == 1
    assert [event.event_type for event in events].count("claimed") == 1
    assert [event.event_type for event in events].count("completed") == 1


def test_ownership_loss_fences_cooperative_copy_without_terminal_write(
    session, settings, inventory_row, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = create_rebuild_run(session, cutoff_at=datetime(2026, 8, 21, tzinfo=UTC))
    enqueue_archive_copy(session, run.id, [inventory_row])
    engine = session.get_bind()
    entered = threading.Event()

    monkeypatch.setattr(campaign, "LEASE_TTL", timedelta(milliseconds=100))
    monkeypatch.setattr(campaign, "LEASE_HEARTBEAT_INTERVAL", 0.01)

    def lose_owner(*args, **kwargs):
        task_id = kwargs["task_id"]
        with Session(engine) as thief_session:
            thief = thief_session.get(PipelineTask, task_id)
            assert thief is not None
            thief.lease_owner = "other-owner"
            thief.lease_expires_at = datetime.now(UTC) + timedelta(seconds=1)
            thief_session.commit()
        return False

    def cooperative_copy(*args, **kwargs):
        entered.set()
        while kwargs["should_continue"]():
            time.sleep(0.005)
        return SimpleNamespace(status="failed", error_code="LEASE_LOST")

    monkeypatch.setattr(campaign, "_renew_lease", lose_owner)
    monkeypatch.setattr(campaign, "copy_inventory_row", cooperative_copy)
    worker = threading.Thread(target=lambda: _execute_in_session(
        engine, settings, run.id, inventory_row,
    ))
    worker.start()
    assert entered.wait(2)
    worker.join(timeout=2)
    assert not worker.is_alive()

    task = session.scalar(select(PipelineTask).where(PipelineTask.run_id == run.id))
    events = session.scalars(select(PipelineEvent).where(PipelineEvent.task_id == task.id)).all()
    assert task.status == "running"
    assert task.lease_owner == "other-owner"
    assert enqueue_archive_copy(session, run.id, [inventory_row]) == 0
    assert [event.event_type for event in events].count("claimed") == 1
    assert [event.event_type for event in events].count("completed") == 0
    assert [event.event_type for event in events].count("failed") == 0


def test_ownership_loss_after_verification_fences_publish_and_later_owner_resumes(
    session, settings, inventory_row, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claimant fenced at the publish boundary leaves retryable local state."""
    run = create_rebuild_run(session, cutoff_at=datetime(2026, 8, 21, tzinfo=UTC))
    enqueue_archive_copy(session, run.id, [inventory_row])
    engine = session.get_bind()
    health = campaign._LeaseHealth()
    original_start = campaign._start_lease_heartbeat
    original_file_matches = archive_copy._file_matches
    match_calls = 0

    def controlled_heartbeat(*args, **kwargs):
        stop = threading.Event()
        thread = threading.Thread(target=stop.wait, daemon=True)
        thread.start()
        return stop, thread, health

    def lose_ownership_after_final_verification(*args, **kwargs):
        nonlocal match_calls
        matches = original_file_matches(*args, **kwargs)
        match_calls += 1
        if match_calls == 3:
            with Session(engine) as thief_session:
                thief = thief_session.scalar(select(PipelineTask).where(
                    PipelineTask.run_id == run.id,
                    PipelineTask.stage == "archive_copy",
                ))
                assert thief is not None
                thief.lease_owner = "later-owner"
                thief.lease_expires_at = datetime.now(UTC) + timedelta(seconds=1)
                thief_session.commit()
            health.lost.set()
        return matches

    monkeypatch.setattr(campaign, "_start_lease_heartbeat", controlled_heartbeat)
    monkeypatch.setattr(archive_copy, "_file_matches", lose_ownership_after_final_verification)

    first_result = execute_archive_copy(session, settings, run.id, [inventory_row])

    session.expire_all()
    task = session.scalar(select(PipelineTask).where(
        PipelineTask.run_id == run.id,
        PipelineTask.stage == "archive_copy",
    ))
    output = session.scalar(select(RebuildOutput).where(RebuildOutput.run_id == run.id))
    events = session.scalars(select(PipelineEvent).where(PipelineEvent.task_id == task.id)).all()
    target = resolve_rebuild_path(settings, inventory_row.destination_relpath)
    assert first_result == {"copied": 0, "failed": 0}
    assert match_calls == 3
    assert not target.exists()
    assert output.status == "pending"
    assert output.error_code is None
    assert task.status == "running"
    assert task.lease_owner == "later-owner"
    assert task.recoverable is True
    assert [event.event_type for event in events if event.event_type in {"completed", "failed"}] == []

    monkeypatch.setattr(campaign, "_start_lease_heartbeat", original_start)
    monkeypatch.setattr(archive_copy, "_file_matches", original_file_matches)
    task.lease_expires_at = datetime(2020, 1, 1, tzinfo=UTC)
    session.commit()

    assert enqueue_archive_copy(session, run.id, [inventory_row]) == 1
    second_result = execute_archive_copy(session, settings, run.id, [inventory_row])

    session.refresh(task)
    session.refresh(output)
    events = session.scalars(select(PipelineEvent).where(PipelineEvent.task_id == task.id)).all()
    assert second_result == {"copied": 1, "failed": 0}
    assert target.read_bytes() == b"synthetic campaign original"
    assert output.status == "success"
    assert output.error_code is None
    assert task.status == "completed"
    assert [event.event_type for event in events].count("claimed") == 2
    assert [event.event_type for event in events].count("completed") == 1
    assert [event.event_type for event in events].count("failed") == 0
