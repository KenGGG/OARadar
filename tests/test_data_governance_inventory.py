"""隐私安全、数据库感知的清理预检测试。"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.data_governance.service import build_cleanup_plan
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import CleanupItem, ItemOccurrence, LogicalItem


def _write(data_root: Path, relpath: str, content: bytes = b"synthetic") -> Path:
    path = data_root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_cleanup_plan_rejects_legacy_runtime_and_projection_trees_under_data_root(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    settings = Settings(app={"data_root": data_root})
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)

    allowed_paths = {
        "originals/2026/08/synthetic/source.pdf",
        "markdown/2026/08/synthetic/source.pdf.md",
    }
    prohibited_candidate_paths = {
        "browser-profile/Default/Cache/cache.bin",
        "browser-profile/Default/Code Cache/js/code.bin",
        "runtime/reports/status.json",
        "parse/rebuildable.md",
        "vault/rebuildable.md",
        "workspace/rebuildable.md",
    }
    for relpath in allowed_paths | prohibited_candidate_paths:
        _write(data_root, relpath)

    summary = build_cleanup_plan(
        settings,
        engine,
        categories={"browser_cache", "runtime_reports", "rebuildable_projection"},
    )

    with Session(engine) as session:
        rows = session.scalars(
            select(CleanupItem).where(CleanupItem.cleanup_run_id == summary.run_id)
        ).all()
    planned = {row.relative_path for row in rows}
    # The two-root contract permits only originals/ and markdown/ below data_root.
    # Runtime/cache and derived-work trees must be outside data_root, never cleanup candidates.
    assert planned == set()
    assert summary.candidate_count == 0
    assert summary.candidate_bytes == 0
    assert set(summary.categories) == {"browser_cache", "runtime_reports", "rebuildable_projection"}
    assert "source.pdf" not in repr(summary)


def test_sent_pending_orphans_excludes_database_references_and_requires_cleaned_ledger(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    settings = Settings(app={"data_root": data_root})
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    orphan = _write(data_root, "raw/pending/old/orphan.pdf")
    _write(data_root, "raw/pending/active/unreferenced.pdf")

    # Without a successful cleanup ledger, unreferenced pending files are not
    # assumed disposable: they might be an in-flight download.
    empty = build_cleanup_plan(settings, engine, categories={"sent_pending_orphans"})
    assert empty.candidate_count == 0

    with Session(engine) as session:
        logical = LogicalItem(logical_key="cleaned-synthetic", title="Synthetic", lifecycle_status="pending")
        session.add(logical)
        session.flush()
        session.add(ItemOccurrence(
            logical_item_id=logical.id, occurrence_key="pending:cleaned", channel="pending",
            occurrence_status="cleaned", cleanup_status="cleaned", raw_fields_json="{}",
        ))
        session.commit()

    summary = build_cleanup_plan(settings, engine, categories={"sent_pending_orphans"})
    with Session(engine) as session:
        rows = session.scalars(
            select(CleanupItem).where(CleanupItem.cleanup_run_id == summary.run_id)
        ).all()
    assert {row.relative_path for row in rows} == {
        orphan.relative_to(data_root).as_posix(),
        "raw/pending/active/unreferenced.pdf",
    }


def test_expired_backups_keep_newest_two_and_one_weekly_baseline(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    settings = Settings(app={"data_root": data_root})
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    # Thursday fixes the ISO-week boundary: the third backup is Monday in the
    # current week and the fourth is Sunday in the previous week.
    now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
    for index, days_old in enumerate((1, 2, 3, 4), start=1):
        path = _write(data_root, f"state/backups/oa-{index}.db")
        timestamp = (now - timedelta(days=days_old)).timestamp()
        os.utime(path, (timestamp, timestamp))
    weekly = _write(data_root, "state/backups/weekly-baseline.db")
    # This baseline belongs to an older ISO week than the Sunday backup above.
    timestamp = (now - timedelta(days=14)).timestamp()
    os.utime(weekly, (timestamp, timestamp))
    expired = _write(data_root, "state/backups/expired.db")
    timestamp = (now - timedelta(days=15)).timestamp()
    os.utime(expired, (timestamp, timestamp))

    summary = build_cleanup_plan(settings, engine, categories={"expired_backups"})

    with Session(engine) as session:
        rows = session.scalars(
            select(CleanupItem).where(CleanupItem.cleanup_run_id == summary.run_id)
        ).all()
    planned = {row.relative_path for row in rows}
    # The newest two and the newest remaining file from each ISO week are
    # retained; only the older file in the two-weeks-ago bucket is eligible.
    assert planned == {expired.relative_to(data_root).as_posix()}
    assert weekly.relative_to(data_root).as_posix() not in planned
