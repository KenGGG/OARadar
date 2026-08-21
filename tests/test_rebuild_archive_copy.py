"""Synthetic tests for verified, atomic rebuild archive copies."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from oa_knowledge.archive.integrity import sha256_file
from oa_knowledge.config import Settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, OAItem, PipelineRun, RebuildOutput
from oa_knowledge.rebuild import archive_copy
from oa_knowledge.rebuild.archive_copy import CopyCancelled, copy_inventory_row
from oa_knowledge.rebuild.inventory import InventoryRow, build_inventory
from oa_knowledge.rebuild.paths import resolve_rebuild_path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    value = Settings(
        app={"data_root": tmp_path / "live-data"},
        rebuild={"target_root": tmp_path / "clean-rebuild"},
    )
    value.data_root.mkdir(parents=True)
    return value


@pytest.fixture
def session(settings: Settings):
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as value:
        yield value


@pytest.fixture
def run_id(session: Session) -> int:
    run = PipelineRun(run_key="synthetic-rebuild-copy", pipeline_type="data_rebuild")
    session.add(run)
    session.commit()
    return run.id


@pytest.fixture
def inventory_row(session: Session, settings: Settings) -> InventoryRow:
    content = b"synthetic original evidence"
    source_relpath = "archive/raw/oa/done/synthetic/original.bin"
    source = settings.data_root / source_relpath
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    item = OAItem(
        oa_item_key="done:synthetic-copy",
        source_channel="done",
        title="Synthetic copy item",
        initiated_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    session.add(item)
    session.flush()
    file = ArchivedFile(
        oa_item_id=item.id,
        original_name="original.bin",
        attachment_key="synthetic-original",
        file_role="direct_attachment",
        source_container_key="root",
        local_relpath=source_relpath,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        download_status="verified",
    )
    session.add(file)
    session.commit()
    return build_inventory(session, settings)[0]


def test_copy_verifies_hash_before_success(
    session: Session, settings: Settings, run_id: int, inventory_row: InventoryRow
) -> None:
    """Changing target bytes after copy must not be reported as success."""
    output = copy_inventory_row(session, settings, inventory_row, run_id=run_id)

    assert output.status == "success"
    target = resolve_rebuild_path(settings, output.target_relpath)
    assert target.stat().st_size == inventory_row.size_bytes
    assert sha256_file(target) == inventory_row.sha256


def test_copy_is_idempotent(
    session: Session, settings: Settings, run_id: int, inventory_row: InventoryRow
) -> None:
    """Repeating a completed request returns its one ledger record."""
    first = copy_inventory_row(session, settings, inventory_row, run_id=run_id)
    second = copy_inventory_row(session, settings, inventory_row, run_id=run_id)

    assert first.status == second.status == "success"
    assert second.id == first.id
    assert [output.id for output in session.scalars(select(RebuildOutput))] == [first.id]


def test_copy_fences_itself_when_lease_health_is_lost(
    session: Session, settings: Settings, run_id: int, inventory_row: InventoryRow
) -> None:
    with pytest.raises(CopyCancelled):
        copy_inventory_row(
            session, settings, inventory_row, run_id=run_id, should_continue=lambda: False,
        )

    assert session.scalar(select(RebuildOutput)) is None
    assert not resolve_rebuild_path(settings, inventory_row.destination_relpath).exists()


def test_empty_copy_rechecks_lease_health_before_publication(
    session: Session, settings: Settings, run_id: int, inventory_row: InventoryRow
) -> None:
    """An empty source still observes cancellation after its zero-chunk copy."""
    source = settings.data_root / inventory_row.source_relpath
    source.write_bytes(b"")
    empty_row = InventoryRow(
        **{
            **inventory_row.__dict__,
            "size_bytes": 0,
            "sha256": hashlib.sha256(b"").hexdigest(),
        }
    )
    health_checks = iter((True, True, False))

    with pytest.raises(CopyCancelled):
        copy_inventory_row(
            session,
            settings,
            empty_row,
            run_id=run_id,
            should_continue=lambda: next(health_checks),
        )

    output = session.scalar(select(RebuildOutput))
    assert output.status == "pending"
    assert output.error_code is None
    assert not resolve_rebuild_path(settings, empty_row.destination_relpath).exists()


def test_stale_cancelled_copier_does_not_downgrade_newer_success(
    session: Session,
    settings: Settings,
    run_id: int,
    inventory_row: InventoryRow,
) -> None:
    """Cancellation is control flow, never a shared terminal ledger write."""
    target = resolve_rebuild_path(settings, inventory_row.destination_relpath)
    checks = 0

    def cancel_after_newer_success() -> bool:
        nonlocal checks
        checks += 1
        if checks != 4:
            return True
        target.write_bytes(b"synthetic original evidence")
        engine = session.get_bind()
        with Session(engine) as newer_session:
            newer = newer_session.scalar(select(RebuildOutput).where(
                RebuildOutput.run_id == run_id,
            ))
            assert newer is not None
            newer.status = "success"
            newer.error_code = None
            newer_session.commit()
        return False

    with pytest.raises(CopyCancelled):
        copy_inventory_row(
            session,
            settings,
            inventory_row,
            run_id=run_id,
            should_continue=cancel_after_newer_success,
        )

    session.expire_all()
    output = session.scalar(select(RebuildOutput).where(RebuildOutput.run_id == run_id))
    assert output.status == "success"
    assert output.error_code is None
    assert target.read_bytes() == b"synthetic original evidence"


@pytest.mark.parametrize("damage", ("deleted", "tampered"))
def test_success_ledger_row_is_reverified_before_reuse(
    session: Session,
    settings: Settings,
    run_id: int,
    inventory_row: InventoryRow,
    damage: str,
) -> None:
    """A stale success row cannot mask a deleted or changed final target."""
    first = copy_inventory_row(session, settings, inventory_row, run_id=run_id)
    target = resolve_rebuild_path(settings, first.target_relpath)
    if damage == "deleted":
        target.unlink()
    else:
        target.write_bytes(b"tampered synthetic target")

    second = copy_inventory_row(session, settings, inventory_row, run_id=run_id)

    assert second.id == first.id
    assert second.status == ("success" if damage == "deleted" else "failed")
    assert second.error_code == (None if damage == "deleted" else "TARGET_CONFLICT")
    assert target.read_bytes() == (
        b"synthetic original evidence" if damage == "deleted" else b"tampered synthetic target"
    )


def test_atomic_no_clobber_race_preserves_a_concurrent_different_target(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    settings: Settings,
    run_id: int,
    inventory_row: InventoryRow,
) -> None:
    """A target created after copying but before publish wins without overwrite."""
    target = resolve_rebuild_path(settings, inventory_row.destination_relpath)
    def concurrent_target_then_exists(source: str | bytes | os.PathLike, destination: str | bytes | os.PathLike, **kwargs) -> None:
        Path(destination).write_bytes(b"concurrent synthetic target")
        raise FileExistsError

    monkeypatch.setattr(archive_copy.os, "link", concurrent_target_then_exists)

    output = copy_inventory_row(session, settings, inventory_row, run_id=run_id)

    assert output.status == "failed"
    assert output.error_code == "TARGET_CONFLICT"
    assert target.read_bytes() == b"concurrent synthetic target"
    assert not list(target.parent.glob(".rebuild-copy-*.tmp"))


def test_final_ledger_persistence_failure_retains_pending_final_for_retry(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    settings: Settings,
    run_id: int,
    inventory_row: InventoryRow,
) -> None:
    """A failed success commit leaves a verified pending final for later recovery."""
    target = resolve_rebuild_path(settings, inventory_row.destination_relpath)
    real_set_status = archive_copy._set_status

    def fail_success_commit(current_session, output, *, status, error_code):
        if status == "success":
            raise RuntimeError("synthetic final ledger persistence failure")
        return real_set_status(current_session, output, status=status, error_code=error_code)

    monkeypatch.setattr(archive_copy, "_set_status", fail_success_commit)

    with pytest.raises(RuntimeError, match="persistence"):
        copy_inventory_row(session, settings, inventory_row, run_id=run_id)

    assert target.read_bytes() == b"synthetic original evidence"
    session.rollback()
    engine = create_db_engine(settings.database_path)
    with Session(engine) as verifier:
        persisted = verifier.scalar(select(RebuildOutput))
    assert persisted is not None
    assert persisted.status == "pending"

    monkeypatch.setattr(archive_copy, "_set_status", real_set_status)
    recovered = copy_inventory_row(session, settings, inventory_row, run_id=run_id)
    assert recovered.id == persisted.id
    assert recovered.status == "success"


def test_copy_does_not_commit_unrelated_caller_session_work(
    session: Session, settings: Settings, run_id: int, inventory_row: InventoryRow
) -> None:
    """The isolated ledger transaction leaves caller-owned changes rollbackable."""
    unrelated = OAItem(
        oa_item_key="uncommitted-synthetic-caller-item",
        source_channel="done",
        title="Uncommitted synthetic item",
    )
    session.add(unrelated)

    output = copy_inventory_row(session, settings, inventory_row, run_id=run_id)

    engine = create_db_engine(settings.database_path)
    with Session(engine) as verifier:
        assert verifier.get(RebuildOutput, output.id) is not None
        assert verifier.scalar(select(OAItem).where(
            OAItem.oa_item_key == "uncommitted-synthetic-caller-item"
        )) is None
    session.rollback()


def test_persistence_failure_before_publish_leaves_no_final_target(
    session: Session, settings: Settings, inventory_row: InventoryRow
) -> None:
    """A foreign-key persistence failure occurs before any final file is published."""
    target = resolve_rebuild_path(settings, inventory_row.destination_relpath)

    with pytest.raises(IntegrityError):
        copy_inventory_row(session, settings, inventory_row, run_id=999_999)

    assert not target.exists()


def test_success_is_durable_after_the_callers_rollback(
    session: Session, settings: Settings, run_id: int, inventory_row: InventoryRow
) -> None:
    """A returned success is committed independently of a later caller rollback."""
    output = copy_inventory_row(session, settings, inventory_row, run_id=run_id)
    output_id = output.id
    session.rollback()

    engine = create_db_engine(settings.database_path)
    with Session(engine) as verifier:
        persisted = verifier.get(RebuildOutput, output_id)
    assert persisted is not None
    assert persisted.status == "success"
    assert resolve_rebuild_path(settings, inventory_row.destination_relpath).is_file()


def test_copy_revalidates_source_before_creating_a_target(
    session: Session, settings: Settings, run_id: int, inventory_row: InventoryRow
) -> None:
    """A source changed after inventory must never be copied as verified evidence."""
    source = settings.data_root / inventory_row.source_relpath
    source.write_bytes(b"changed after inventory")

    output = copy_inventory_row(session, settings, inventory_row, run_id=run_id)

    assert output.status == "failed"
    assert output.error_code == "SOURCE_SIZE_MISMATCH"
    assert not resolve_rebuild_path(settings, inventory_row.destination_relpath).exists()


def test_copy_never_replaces_a_different_existing_target(
    session: Session, settings: Settings, run_id: int, inventory_row: InventoryRow
) -> None:
    """An occupied target with different bytes is retained and recorded as a conflict."""
    target = resolve_rebuild_path(settings, inventory_row.destination_relpath)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"different pre-existing synthetic evidence")

    output = copy_inventory_row(session, settings, inventory_row, run_id=run_id)

    assert output.status == "failed"
    assert output.error_code == "TARGET_CONFLICT"
    assert target.read_bytes() == b"different pre-existing synthetic evidence"
    assert not list(target.parent.glob(".rebuild-copy-*.tmp"))


def test_copy_rejects_nonready_inventory_rows(
    session: Session, settings: Settings, run_id: int, inventory_row: InventoryRow
) -> None:
    """Only inventory evidence explicitly marked ready can reach the copier."""
    blocked = InventoryRow(
        **{**inventory_row.__dict__, "status": "hash_mismatch"}
    )

    with pytest.raises(ValueError, match="ready"):
        copy_inventory_row(session, settings, blocked, run_id=run_id)
