"""Synthetic evidence tests for the local rebuild inventory."""

from __future__ import annotations

import hashlib
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import (
    ArchivedFile,
    BatchItem,
    CollectionBatch,
    OAItem,
    OAManifestItem,
)
from oa_knowledge.rebuild.inventory import (
    InventoryRow,
    build_inventory,
    inventory_summary,
    write_private_inventory,
)
from oa_knowledge.rebuild.paths import archive_file_relpath


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    settings = Settings(
        app={"data_root": tmp_path / "data"},
        rebuild={"item_title_max_chars": 8},
    )
    settings.data_root.mkdir(parents=True)
    return settings


@pytest.fixture
def session(settings: Settings):
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        yield session


def _add_file(
    session: Session,
    settings: Settings,
    *,
    name: str,
    relpath: str,
    content: bytes | None,
    recorded_sha256: str | None = None,
    depth_limit: bool = False,
) -> tuple[OAItem, ArchivedFile]:
    item = OAItem(
        oa_item_key=f"done:{name}",
        source_channel="done",
        title="合成事项标题超出限制",
        initiated_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    session.add(item)
    session.flush()
    session.add(
        OAManifestItem(
            oa_item_key=item.oa_item_key,
            title=item.title,
            list_page=0,
            processing_status="depth_limit_reached" if depth_limit else "downloaded",
        )
    )
    if content is not None:
        source = settings.data_root / relpath
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
    digest = recorded_sha256 or hashlib.sha256(content or b"missing").hexdigest()
    archived = ArchivedFile(
        oa_item_id=item.id,
        original_name=f"{name}.bin",
        attachment_key=name,
        file_role="direct_attachment",
        source_container_key="root",
        local_relpath=relpath,
        size_bytes=len(content or b"missing"),
        sha256=digest,
        download_status="verified",
    )
    session.add(archived)
    session.flush()
    return item, archived


def test_inventory_admits_only_verified_hash_matches(
    session: Session, settings: Settings
) -> None:
    item, archived_file = _add_file(
        session,
        settings,
        name="verified",
        relpath="archive/raw/oa/done/synthetic/verified.bin",
        content=b"verified",
    )

    rows = build_inventory(session, settings)

    row = next(row for row in rows if row.file_id == archived_file.id)
    assert row.status == "ready"
    assert (
        row.destination_relpath
        == archive_file_relpath(
            item,
            archived_file,
            item_title_max_chars=settings.rebuild.item_title_max_chars,
        ).as_posix()
    )


@pytest.mark.parametrize(
    ("name", "relpath", "content", "recorded_sha256", "depth_limit", "expected_status"),
    [
        (
            "missing",
            "archive/raw/oa/done/synthetic/missing.bin",
            None,
            None,
            False,
            "missing",
        ),
        (
            "mismatch",
            "archive/raw/oa/done/synthetic/mismatch.bin",
            b"actual",
            "0" * 64,
            False,
            "hash_mismatch",
        ),
        ("unsafe", "../outside.bin", b"never read", None, False, "unsafe_path"),
        (
            "limited",
            "archive/raw/oa/done/synthetic/limited.bin",
            b"verified",
            None,
            True,
            "depth_limit_reached",
        ),
    ],
)
def test_inventory_marks_nonready_evidence_explicitly(
    session: Session,
    settings: Settings,
    name: str,
    relpath: str,
    content: bytes | None,
    recorded_sha256: str | None,
    depth_limit: bool,
    expected_status: str,
) -> None:
    _, archived_file = _add_file(
        session,
        settings,
        name=name,
        relpath=relpath,
        content=content,
        recorded_sha256=recorded_sha256,
        depth_limit=depth_limit,
    )

    rows = build_inventory(session, settings)

    assert (
        next(row for row in rows if row.file_id == archived_file.id).status
        == expected_status
    )


def test_inventory_honors_batch_depth_limit_without_a_manifest_flag(
    session: Session, settings: Settings
) -> None:
    item, archived_file = _add_file(
        session,
        settings,
        name="batch-limited",
        relpath="archive/raw/oa/done/synthetic/limited.bin",
        content=b"verified",
    )
    batch = CollectionBatch(
        batch_key="synthetic-batch",
        source_channel="done",
        planned_limit=1,
        plan_hash="a" * 64,
    )
    session.add(batch)
    session.flush()
    session.add(
        BatchItem(
            batch_id=batch.id,
            oa_item_key=item.oa_item_key,
            workitem_id_text="synthetic",
            title=item.title,
            ordinal=0,
            discovery_status="discovered",
            archive_status="depth_limit_reached",
            oa_item_id=item.id,
        )
    )
    session.flush()

    rows = build_inventory(session, settings)

    assert (
        next(row for row in rows if row.file_id == archived_file.id).status
        == "depth_limit_reached"
    )


def test_inventory_summary_returns_counts_without_printing_confidential_paths(
    capsys,
) -> None:
    rows = [
        InventoryRow(
            1,
            2,
            "confidential/source.bin",
            "archive/destination.bin",
            1,
            "a" * 64,
            "attachment",
            "ready",
        ),
        InventoryRow(
            1,
            3,
            "confidential/missing.bin",
            "archive/missing.bin",
            1,
            "b" * 64,
            "attachment",
            "missing",
        ),
    ]

    assert inventory_summary(rows) == {"ready": 1, "missing": 1, "total": 2}
    assert capsys.readouterr().out == ""


def test_private_inventory_is_confined_and_owner_readable_only(tmp_path: Path) -> None:
    rows = [
        InventoryRow(
            1,
            2,
            "source.bin",
            "archive/destination.bin",
            1,
            "a" * 64,
            "attachment",
            "ready",
        )
    ]
    target = tmp_path / "data_rebuilt" / "state" / "private" / "inventory.json"

    write_private_inventory(target, rows)

    assert target.is_file()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert '"source_relpath": "source.bin"' in target.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="state/private"):
        write_private_inventory(tmp_path / "elsewhere.json", rows)
