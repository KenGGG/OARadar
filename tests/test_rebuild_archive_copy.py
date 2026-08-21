"""Synthetic tests for verified, atomic rebuild archive copies."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.archive.integrity import sha256_file
from oa_knowledge.config import Settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, OAItem, PipelineRun, RebuildOutput
from oa_knowledge.rebuild.archive_copy import copy_inventory_row
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
    session.flush()
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
    session.flush()
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
    assert session.scalars(select(RebuildOutput)).all() == [first]


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
