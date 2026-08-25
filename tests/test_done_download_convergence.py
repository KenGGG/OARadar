from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import OAManifestItem, OperationJob
from oa_knowledge.web.worker import OperationWorker
from oa_knowledge.cli import is_systemic_browser_closed
from oa_knowledge.web.simple_status import _done_summary
from oa_knowledge.archive_paths import done_archive_collision_directory


def test_manifest_retry_snapshot_never_uses_progress_as_a_target_offset(config_file: Path) -> None:
    """Changing a saved progress integer must not skip an unattempted key."""
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    started = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add_all([
            OAManifestItem(oa_item_key="done:terminal", title="合成终态", list_page=1,
                           processing_status="downloaded"),
            OAManifestItem(oa_item_key="done:pending", title="合成待下载", list_page=1,
                           processing_status="pending_download"),
            OAManifestItem(oa_item_key="done:attempted", title="合成已尝试", list_page=1,
                           processing_status="download_failed", last_retry_at=started + timedelta(seconds=1)),
        ])
        job = OperationJob(
            job_key="synthetic-retry", job_type="full_manifest_retry", status="running",
            idempotency_key="synthetic-retry", progress_current=2, progress_total=3,
            started_at=started, parameters_json=json.dumps({
                "oa_item_keys": ["done:terminal", "done:pending", "done:attempted"],
                "source_status": "pending_download",
            }),
        )
        session.add(job)
        session.commit()
        job_id = job.id

    worker = OperationWorker(settings, config_path=config_file)
    try:
        snapshot = worker._manifest_retry_snapshot(job_id)
    finally:
        worker.close()

    assert snapshot.total == 3
    assert snapshot.success == 1
    assert snapshot.failed == 1
    assert snapshot.pending_keys == ("done:pending",)


def test_manifest_retry_snapshot_requires_all_targets_before_completed(config_file: Path) -> None:
    """A child process exit cannot make a partially attempted retry complete."""
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    started = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add_all([
            OAManifestItem(oa_item_key="done:ok", title="合成完成", list_page=1,
                           processing_status="downloaded"),
            OAManifestItem(oa_item_key="done:later", title="合成未尝试", list_page=1,
                           processing_status="pending_download"),
        ])
        job = OperationJob(
            job_key="synthetic-incomplete", job_type="full_manifest_retry", status="running",
            idempotency_key="synthetic-incomplete", progress_current=2, progress_total=2,
            started_at=started, parameters_json=json.dumps({
                "oa_item_keys": ["done:ok", "done:later"], "source_status": "pending_download",
            }),
        )
        session.add(job)
        session.commit()
        job_id = job.id

    worker = OperationWorker(settings, config_path=config_file)
    try:
        snapshot = worker._manifest_retry_snapshot(job_id)
    finally:
        worker.close()

    assert snapshot.progress_current == 1
    assert snapshot.complete is False
    assert snapshot.pending_keys == ("done:later",)


def test_systemic_browser_closed_detection_does_not_treat_normal_download_errors_as_global() -> None:
    """Only a closed Playwright target stops the whole download chunk."""
    assert is_systemic_browser_closed(RuntimeError("Target page, context or browser has been closed"))
    assert is_systemic_browser_closed(RuntimeError("TargetClosedError: Locator.count"))
    assert not is_systemic_browser_closed(RuntimeError("synthetic attachment download timeout"))


def test_done_summary_reports_download_queue_and_download_issues_separately(config_file: Path) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        session.add_all([
            OAManifestItem(oa_item_key="done:ok", title="合成完成", list_page=1,
                           processing_status="downloaded"),
            OAManifestItem(oa_item_key="done:pending", title="合成待下载", list_page=1,
                           processing_status="pending_download"),
            OAManifestItem(oa_item_key="done:failed", title="合成失败", list_page=1,
                           processing_status="download_failed"),
            OAManifestItem(oa_item_key="done:excluded", title="合成排除", list_page=1,
                           processing_status="skipped"),
        ])
        session.commit()
        summary = _done_summary(session, {})

    assert summary["waiting_download_items"] == 1
    assert summary["download_issue_items"] == 1
    # 原件已下载、但尚无 Markdown 交付时，才计入待 MD 化。
    assert summary["queued_items"] == 1


def test_done_collision_path_stays_under_originals_and_is_stable() -> None:
    created_at = datetime(2026, 8, 25, tzinfo=timezone.utc)
    first = done_archive_collision_directory("同名事项", "done:one", created_at)
    second = done_archive_collision_directory("同名事项", "done:two", created_at)

    assert first.parts[0] == "originals"
    assert first != second
    assert first == done_archive_collision_directory("同名事项", "done:one", created_at)
