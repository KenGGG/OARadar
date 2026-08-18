"""隐私安全、数据库感知的清理预检测试。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.data_governance.service import build_cleanup_plan
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import CleanupItem, ItemOccurrence, LogicalItem, PipelineTask, ReviewEntry


def _write(data_root: Path, relpath: str, content: bytes = b"synthetic") -> Path:
    path = data_root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_cleanup_plan_selects_only_rebuildable_unprotected_files(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    settings = Settings(app={"data_root": data_root})
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)

    candidate_paths = {
        "runtime/browser-profile/Default/Cache/cache.bin",
        "runtime/browser-profile/Default/Code Cache/js/code.bin",
        "runtime/reports/status.json",
        "parse/rebuildable.md",
    }
    protected_paths = {
        "raw/done/2026/original.pdf",
        "runtime/browser-profile/Default/Cookies",
        "runtime/browser-profile/Default/Local Storage/session.bin",
        "runtime/browser-profile/Default/Sessions/tab.bin",
        "parse/active-input.md",
        "parse/review-required.md",
    }
    for relpath in candidate_paths | protected_paths:
        _write(data_root, relpath)

    with Session(engine) as session:
        session.add(PipelineTask(
            queue_name="done", priority=1, logical_item_key="synthetic", stage="parse",
            status="running", idempotency_key="active-task",
            payload_json=json.dumps({"source_relpath": "parse/active-input.md"}),
        ))
        session.add(ReviewEntry(
            kind="hash_mismatch", details_json=json.dumps({"relative_path": "parse/review-required.md"}),
            status="pending",
        ))
        session.commit()

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
    assert planned == candidate_paths
    assert summary.candidate_count == len(candidate_paths)
    assert summary.candidate_bytes == sum((data_root / path).stat().st_size for path in candidate_paths)
    assert set(summary.categories) == {"browser_cache", "runtime_reports", "rebuildable_projection"}
    assert "original.pdf" not in repr(summary)


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
    now = datetime.now(timezone.utc)
    same_week: list[Path] = []
    for index, days_old in enumerate((1, 2, 3, 4), start=1):
        path = _write(data_root, f"state/backups/oa-{index}.db")
        timestamp = (now - timedelta(days=days_old)).timestamp()
        os.utime(path, (timestamp, timestamp))
        same_week.append(path)
    weekly = _write(data_root, "state/backups/weekly-baseline.db")
    timestamp = (now - timedelta(days=10)).timestamp()
    os.utime(weekly, (timestamp, timestamp))

    summary = build_cleanup_plan(settings, engine, categories={"expired_backups"})

    with Session(engine) as session:
        rows = session.scalars(
            select(CleanupItem).where(CleanupItem.cleanup_run_id == summary.run_id)
        ).all()
    planned = {row.relative_path for row in rows}
    # The newest two are always retained; the newest remaining file in the
    # current ISO week is retained as its weekly baseline.
    assert planned == {same_week[3].relative_to(data_root).as_posix()}
    assert weekly.relative_to(data_root).as_posix() not in planned
