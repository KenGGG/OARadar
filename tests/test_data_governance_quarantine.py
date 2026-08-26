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
