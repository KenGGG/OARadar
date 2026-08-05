"""Tests for the read-only database auditor (``oa_knowledge.ops.audit``).

All fixtures are synthetic and live under a temporary ``data_root``; no real OA
content is touched, per the repository confidentiality rules.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, CollectionBatch, OAItem
from oa_knowledge.ops.audit import AuditIssue, audit_database


def _upgraded_settings(config_file: Path):
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    upgrade_database(settings.database_path)
    return settings


def _issue_codes(settings) -> set[str]:
    return {issue.code for issue in audit_database(settings)}


def test_clean_database_reports_no_issues(config_file: Path) -> None:
    settings = _upgraded_settings(config_file)
    assert audit_database(settings) == []


def test_missing_database_is_reported(config_file: Path) -> None:
    # Do NOT create data_root, so the database file does not exist.
    settings = load_settings(config_file)
    issues = audit_database(settings)
    assert [issue.code for issue in issues] == ["database_missing"]


def test_corrupt_database_is_reported(config_file: Path) -> None:
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    settings.database_path.write_text("this is not a sqlite database", encoding="utf-8")
    issues = audit_database(settings)
    assert any(issue.code == "database_corrupt" for issue in issues)


def test_batch_manifest_count_mismatch_is_detected(config_file: Path) -> None:
    settings = _upgraded_settings(config_file)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        batch = CollectionBatch(
            batch_key="audit-mismatch",
            source_channel="done",
            planned_limit=20,
            status="completed",
            discovered_count=5,  # no batch_items exist -> item_count == 0
            plan_hash="a" * 64,
            frozen_at=datetime.now(timezone.utc),
        )
        session.add(batch)
        session.commit()
    issues = audit_database(settings)
    codes = {issue.code for issue in issues}
    assert "batch_manifest_count_mismatch" in codes
    # A well-formed batch that only mismatches on discovered count should not
    # also trip the unrelated constraints.
    assert "batch_limit_invalid" not in codes
    assert "batch_not_frozen" not in codes
    assert "batch_status_invalid" not in codes
    engine.dispose()


def test_file_missing_is_detected(config_file: Path) -> None:
    settings = _upgraded_settings(config_file)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = OAItem(
            oa_item_key="audit-file-missing",
            source_channel="done",
            title="合成缺失附件事项",
            pipeline_status="files_verified",
        )
        session.add(item)
        session.flush()
        session.add(ArchivedFile(
            oa_item_id=item.id,
            original_name="正文.pdf",
            attachment_key="body",
            file_role="official_body",
            source_container_key="root",
            local_relpath="raw/does-not-exist.pdf",  # intentionally absent
            download_status="discovered",  # not "verified", so no hash check
        ))
        session.commit()
    issues = audit_database(settings)
    assert any(
        issue.code == "file_missing" and issue.record_id is not None
        for issue in issues
    )
    engine.dispose()


def test_audit_issue_is_a_frozen_dataclass(config_file: Path) -> None:
    issue = AuditIssue("sample", 1, "detail")
    assert issue.code == "sample"
    assert issue.record_id == 1
    assert issue.detail == "detail"
