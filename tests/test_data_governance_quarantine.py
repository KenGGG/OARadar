"""数据治理隔离、恢复和永久清除测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.data_governance.quarantine import purge_run, quarantine_run, restore_run
from oa_knowledge.data_governance.service import build_cleanup_plan
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import CleanupItem, CleanupRun


DISABLED_CATEGORY_PATHS = (
    ("browser_cache", "browser-profile/Default/Cache/cache.bin"),
    ("runtime_reports", "runtime/reports/status.json"),
    ("rebuildable_projection", "parse/projection.md"),
)


def _prepared_plan(tmp_path: Path):
    settings = Settings(app={"data_root": tmp_path / "data"})
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    backup_root = settings.data_root / "runtime/backups"
    source = backup_root / "synthetic.json"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic-report")
    modified = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
    for hours, name in enumerate(("newest.db", "second.db", "weekly.db")):
        backup = backup_root / name
        backup.write_bytes(name.encode())
        timestamp = (modified - timedelta(hours=hours)).timestamp()
        os.utime(backup, (timestamp, timestamp))
    source_timestamp = (modified - timedelta(hours=3)).timestamp()
    os.utime(source, (source_timestamp, source_timestamp))
    plan = build_cleanup_plan(settings, engine, categories={"expired_backups"})
    assert plan.candidate_count == 1
    return settings, engine, source, plan


def _stale_disabled_plan(
    tmp_path: Path,
    category: str,
    relative_path: str,
    *,
    quarantined: bool,
):
    settings = Settings(app={"data_root": tmp_path / "data"})
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    payload = b"synthetic-stale-plan"
    with Session(engine) as session:
        run = CleanupRun(
            status="quarantined" if quarantined else "planned",
            rules_version="legacy-rootless-v1",
            categories_json=f'["{category}"]',
            candidate_count=1,
            candidate_bytes=len(payload),
            finished_at=(datetime.now(timezone.utc) - timedelta(days=8)) if quarantined else None,
        )
        session.add(run)
        session.flush()
        quarantine_relpath = f"quarantine/{run.id}/{relative_path}" if quarantined else None
        session.add(CleanupItem(
            cleanup_run_id=run.id,
            relative_path=relative_path,
            category=category,
            size_bytes=len(payload),
            preflight_sha256="0" * 64,
            status="quarantined" if quarantined else "planned",
            reason_code="legacy_rootless_candidate",
            quarantine_relpath=quarantine_relpath,
        ))
        session.commit()
        run_id = run.id
    stored_relpath = quarantine_relpath if quarantined else relative_path
    stored = settings.data_root / stored_relpath
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(payload)
    return settings, engine, run_id, stored


@pytest.mark.parametrize("category,relative_path", DISABLED_CATEGORY_PATHS)
def test_quarantine_rejects_stale_disabled_category_plan(
    tmp_path: Path, category: str, relative_path: str,
) -> None:
    settings, engine, run_id, source = _stale_disabled_plan(
        tmp_path, category, relative_path, quarantined=False,
    )

    with pytest.raises(ValueError, match="root-qualified"):
        quarantine_run(settings, engine, run_id)

    assert source.read_bytes() == b"synthetic-stale-plan"
    assert not (settings.data_root / f"quarantine/{run_id}").exists()
    with Session(engine) as session:
        assert session.get(CleanupRun, run_id).status == "planned"
        assert session.scalar(select(CleanupItem).where(
            CleanupItem.cleanup_run_id == run_id,
        )).status == "planned"


@pytest.mark.parametrize("category,relative_path", DISABLED_CATEGORY_PATHS)
def test_restore_rejects_stale_disabled_category_plan(
    tmp_path: Path, category: str, relative_path: str,
) -> None:
    settings, engine, run_id, tombstone = _stale_disabled_plan(
        tmp_path, category, relative_path, quarantined=True,
    )

    with pytest.raises(ValueError, match="root-qualified"):
        restore_run(settings, engine, run_id)

    assert tombstone.read_bytes() == b"synthetic-stale-plan"
    assert not (settings.data_root / relative_path).exists()
    with Session(engine) as session:
        assert session.get(CleanupRun, run_id).status == "quarantined"


@pytest.mark.parametrize("category,relative_path", DISABLED_CATEGORY_PATHS)
def test_purge_rejects_stale_disabled_category_tombstone(
    tmp_path: Path, category: str, relative_path: str,
) -> None:
    settings, engine, run_id, tombstone = _stale_disabled_plan(
        tmp_path, category, relative_path, quarantined=True,
    )

    with pytest.raises(ValueError, match="root-qualified"):
        purge_run(
            settings, engine, run_id,
            confirmation=f"PURGE-CLEANUP-RUN-{run_id}",
        )

    assert tombstone.read_bytes() == b"synthetic-stale-plan"
    with Session(engine) as session:
        assert session.get(CleanupRun, run_id).status == "quarantined"
        assert session.scalar(select(CleanupItem).where(
            CleanupItem.cleanup_run_id == run_id,
        )).status == "quarantined"


def test_quarantine_and_restore_move_file_without_overwrite(tmp_path: Path) -> None:
    settings, engine, source, plan = _prepared_plan(tmp_path)

    moved = quarantine_run(settings, engine, plan.run_id)
    assert moved.succeeded_count == 1
    assert not source.exists()
    with Session(engine) as session:
        item = session.scalar(select(CleanupItem).where(CleanupItem.cleanup_run_id == plan.run_id))
        quarantine_path = settings.data_root / item.quarantine_relpath
        assert item.status == "quarantined"
        assert quarantine_path.read_bytes() == b"synthetic-report"
        manifest = settings.data_root / f"quarantine/{plan.run_id}/manifest.json"
        assert manifest.stat().st_mode & 0o777 == 0o600
        assert "synthetic.json" not in manifest.read_text(encoding="utf-8")

    restored = restore_run(settings, engine, plan.run_id)
    assert restored.succeeded_count == 1
    assert source.read_bytes() == b"synthetic-report"


def test_quarantine_skips_file_changed_after_preflight(tmp_path: Path) -> None:
    settings, engine, source, plan = _prepared_plan(tmp_path)
    source.write_bytes(b"changed-after-plan")

    result = quarantine_run(settings, engine, plan.run_id)

    assert result.skipped_count == 1
    assert source.exists()
    with Session(engine) as session:
        item = session.scalar(select(CleanupItem).where(CleanupItem.cleanup_run_id == plan.run_id))
        assert item.status == "skipped"
        assert item.error_code == "preflight_changed"


def test_quarantine_refuses_target_conflict(tmp_path: Path) -> None:
    settings, engine, source, plan = _prepared_plan(tmp_path)
    conflict = settings.data_root / f"quarantine/{plan.run_id}" / source.relative_to(settings.data_root)
    conflict.parent.mkdir(parents=True)
    conflict.write_bytes(b"do-not-overwrite")

    result = quarantine_run(settings, engine, plan.run_id)

    assert result.failed_count == 1
    assert source.exists()
    assert conflict.read_bytes() == b"do-not-overwrite"


def test_restore_refuses_to_overwrite_recreated_source(tmp_path: Path) -> None:
    settings, engine, source, plan = _prepared_plan(tmp_path)
    quarantine_run(settings, engine, plan.run_id)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"new-output")

    result = restore_run(settings, engine, plan.run_id)

    assert result.failed_count == 1
    assert source.read_bytes() == b"new-output"


def test_purge_requires_exact_confirmation_and_elapsed_quarantine(tmp_path: Path) -> None:
    settings, engine, source, plan = _prepared_plan(tmp_path)
    quarantine_run(settings, engine, plan.run_id)

    with pytest.raises(ValueError, match="confirmation"):
        purge_run(settings, engine, plan.run_id, confirmation="wrong")
    with pytest.raises(ValueError, match="retention"):
        purge_run(
            settings, engine, plan.run_id,
            confirmation=f"PURGE-CLEANUP-RUN-{plan.run_id}",
        )

    with Session(engine) as session:
        run = session.get(CleanupRun, plan.run_id)
        run.finished_at = datetime.now(timezone.utc) - timedelta(days=8)
        session.commit()

    result = purge_run(
        settings, engine, plan.run_id,
        confirmation=f"PURGE-CLEANUP-RUN-{plan.run_id}",
    )
    assert result.succeeded_count == 1
    with Session(engine) as session:
        item = session.scalar(select(CleanupItem).where(CleanupItem.cleanup_run_id == plan.run_id))
        assert item.status == "purged"
        assert not (settings.data_root / item.quarantine_relpath).exists()
