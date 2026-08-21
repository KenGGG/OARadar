"""Synthetic orchestration tests for the local clean-archive campaign."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, OAItem, PipelineTask, RebuildOutput
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
