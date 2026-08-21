"""数据治理持久化模型与数据库约束测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import CleanupItem, CleanupRun


def test_data_governance_migration_is_current_and_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "oa.db"

    upgrade_database(database)
    upgrade_database(database)

    with sqlite3.connect(database) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert version == "0036_rebuild_classification_gate"
    assert {"cleanup_runs", "cleanup_items"} <= tables


def test_cleanup_item_path_is_unique_within_run(tmp_path: Path) -> None:
    database = tmp_path / "oa.db"
    upgrade_database(database)
    engine = create_db_engine(database)

    with Session(engine) as session:
        run = CleanupRun(status="planned", rules_version="data-v1", categories_json='["browser_cache"]')
        session.add(run)
        session.flush()
        session.add_all([
            CleanupItem(
                cleanup_run_id=run.id,
                relative_path="runtime/browser/Default/Cache/a.bin",
                category="browser_cache",
                size_bytes=10,
                preflight_sha256="a" * 64,
                status="planned",
                reason_code="rebuildable_cache",
            ),
            CleanupItem(
                cleanup_run_id=run.id,
                relative_path="runtime/browser/Default/Cache/a.bin",
                category="browser_cache",
                size_bytes=10,
                preflight_sha256="a" * 64,
                status="planned",
                reason_code="rebuildable_cache",
            ),
        ])
        with pytest.raises(IntegrityError):
            session.commit()


@pytest.mark.parametrize(
    "relative_path",
    ("/tmp/a.bin", "../a.bin", "raw/pending/../done/a.bin", "./runtime/a.bin"),
)
def test_cleanup_item_rejects_non_relative_or_traversal_path(
    tmp_path: Path, relative_path: str,
) -> None:
    database = tmp_path / "oa.db"
    upgrade_database(database)
    engine = create_db_engine(database)

    with Session(engine) as session:
        run = CleanupRun(status="planned", rules_version="data-v1", categories_json="[]")
        session.add(run)
        session.flush()
        session.add(CleanupItem(
            cleanup_run_id=run.id,
            relative_path=relative_path,
            category="browser_cache",
            size_bytes=1,
            status="planned",
            reason_code="rebuildable_cache",
        ))
        with pytest.raises(IntegrityError):
            session.commit()


def test_cleanup_item_rejects_unknown_status(tmp_path: Path) -> None:
    database = tmp_path / "oa.db"
    upgrade_database(database)
    engine = create_db_engine(database)

    with Session(engine) as session:
        run = CleanupRun(status="planned", rules_version="data-v1", categories_json="[]")
        session.add(run)
        session.flush()
        session.add(CleanupItem(
            cleanup_run_id=run.id,
            relative_path="runtime/reports/report.json",
            category="runtime_reports",
            size_bytes=1,
            status="invented",
            reason_code="rebuildable_report",
        ))
        with pytest.raises(IntegrityError):
            session.commit()
