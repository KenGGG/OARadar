import hashlib
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, BatchItem, CollectionBatch, ContentObject, ItemOccurrence, MarkdownQueueControl, MarkdownTask, NotificationDelivery, OAItem, OAManifestItem, OAManifestSync, OnlineAuditItem, OnlineAuditRun, OperationJob, ParseArtifact, ParseJob, PipelineTask, ReviewEntry
from oa_knowledge.online_audit import start_audit
from oa_knowledge.web.worker import OperationWorker, _has_verified_attachment
from oa_knowledge.production_pipeline import ProductionQueue
from oa_knowledge.notifications.models import DeliveryResult
from oa_knowledge.archive_migration_campaign import ensure_verified_archive_migration


def _record_full_manifest_sync(session: Session, count: int) -> None:
    now = datetime.now(timezone.utc)
    session.add(OAManifestSync(
        oa_total_count=count, local_manifest_count=count,
        pages_scanned=max(1, count), source_total_pages=max(1, count),
        status="manifest_complete", started_at=now, finished_at=now,
    ))


def test_retry_progress_keeps_original_total_after_resume() -> None:
    assert OperationWorker._retry_progress(total_targets=2393, resumed=1118, completed_after_resume=1) == 1119


def test_completed_scheduled_scan_progress_is_never_greater_than_total() -> None:
    assert OperationWorker._completed_scan_progress(
        {"source_total": 13, "created": 13, "updated": 0},
        {"new_items": 25},
    ) == (38, 38)


def test_oa_login_writes_a_live_logging_in_status_before_browser_access(config_file: Path) -> None:
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    runtime_status = settings.state_root / "runtime" / "operation-worker.json"
    worker = OperationWorker(settings, config_path=config_file)

    class Browser:
        def login_with_saved_credentials(self, _timeout: int) -> str:
            payload = json.loads(runtime_status.read_text(encoding="utf-8"))
            assert payload["status"] == "logging_in"
            assert payload["activity"] == "正在验证 OA 登录"
            return "authenticated"

    try:
        assert worker._verify_oa_login(Browser()) == "authenticated"
        payload = json.loads(runtime_status.read_text(encoding="utf-8"))
        assert payload["status"] == "working"
        assert not (settings.data_root / "runtime").exists()
    finally:
        worker.close()


def test_full_manifest_job_runs_list_and_download_pipeline(config_file: Path, monkeypatch) -> None:
    """A full-manifest Web job must not use the download-only, gated command."""
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        job = OperationJob(
            job_key="full-manifest-test", job_type="full_manifest", status="queued",
            idempotency_key="full-manifest-test", parameters_json="{}",
        )
        session.add(job)
        session.commit()
        job_id = job.id

    commands: list[list[str]] = []

    def complete_pipeline(command, _cwd, heartbeat, poll_interval=5.0):
        commands.append(command)
        heartbeat()
        return 0, json.dumps({"oa_total_count": 3, "manifest_status": "manifest_complete"}), ""

    monkeypatch.setattr("oa_knowledge.web.worker._run_piped", complete_pipeline)
    worker = OperationWorker(settings, config_path=config_file)
    try:
        worker._execute_full_manifest(job_id)
    finally:
        worker.close()

    with Session(engine) as session:
        job = session.get(OperationJob, job_id)
        assert job is not None
        assert job.status == "completed"
        assert job.progress_total == 3
    assert commands == [[
        __import__("sys").executable, "-m", "oa_knowledge.cli", "manifest", "run",
        "--config", str(config_file),
    ]]


def test_long_pipeline_stage_refreshes_its_database_lease(config_file: Path, monkeypatch) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    queue = ProductionQueue(engine)
    queue.enqueue("realtime_pending", "synthetic-lease", "pending_parse", "lease-heartbeat")
    task = queue.claim("worker-test")
    initial_expiry = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with Session(engine) as session:
        row = session.get(PipelineTask, task.id)
        row.lease_expires_at = initial_expiry
        session.commit()

    worker = OperationWorker(settings, config_path=config_file)
    worker.owner = "worker-test"
    monkeypatch.setattr("oa_knowledge.web.worker.PIPELINE_HEARTBEAT_SECONDS", 0.01, raising=False)
    observed = {"refreshed": False}

    def slow_stage(_task) -> None:
        deadline = time.monotonic() + 0.25
        while time.monotonic() < deadline:
            with Session(engine) as session:
                expiry = session.get(PipelineTask, task.id).lease_expires_at
                if expiry and expiry.replace(tzinfo=timezone.utc) > initial_expiry:
                    observed["refreshed"] = True
                    return
            time.sleep(0.01)

    monkeypatch.setattr(worker, "_pipeline_pending_parse", slow_stage)
    try:
        worker._execute_pipeline_task(task)
    finally:
        worker.close()

    assert observed["refreshed"] is True


def test_has_verified_attachment_returns_boolean_for_empty_and_existing_sources(config_file: Path) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = OAItem(oa_item_key="done:no-new-attachments", source_channel="done", title="合成事项")
        session.add(item); session.flush()
        assert _has_verified_attachment(session, item.oa_item_key) is False
        session.add(ArchivedFile(
            oa_item_id=item.id, attachment_key="synthetic", original_name="synthetic.txt",
            file_role="direct_attachment", source_container_key="root",
            download_status="verified",
        ))
        session.flush()
        assert _has_verified_attachment(session, item.oa_item_key) is True


def test_online_audit_browser_start_failure_updates_run_and_job_together(config_file: Path, monkeypatch) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    run_id = start_audit(settings)["run_id"]

    class BrokenBrowser:
        def __init__(self, *_args, **_kwargs): pass
        def __enter__(self): raise RuntimeError("synthetic browser start failure")
        def __exit__(self, *_args): return None

    monkeypatch.setattr("oa_knowledge.collector.browser.BrowserSession", BrokenBrowser)
    worker = OperationWorker(settings, config_path=config_file)
    try:
        assert worker.run_once() is True
    finally:
        worker.close()

    with Session(engine) as session:
        run = session.get(OnlineAuditRun, run_id)
        job = session.get(OperationJob, run.job_id)
        assert run.status == "failed"
        assert job.status == "failed"


def test_online_audit_uses_bounded_audit_timeouts_for_each_oa_item(config_file: Path, monkeypatch) -> None:
    """A slow audit item must not inherit the much longer archive timeouts."""
    from oa_knowledge.collector import LoginState

    settings = load_settings(config_file)
    settings.online_audit.item_timeout_seconds = 73
    settings.online_audit.download_timeout_seconds = 17
    settings.collector.attachment_total_timeout_seconds = 901
    settings.collector.download_timeout_seconds = 181
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        session.add(OAManifestItem(
            oa_item_key="done:bounded-audit", workitem_id_text="bounded-audit",
            title="合成已办", list_page=1, processing_status="downloaded",
        ))
        session.commit()
    run_id = start_audit(settings)["run_id"]
    observed: dict[str, int] = {}

    class FakeBrowser:
        page = MagicMock()
        base_url = "http://oa.invalid"

        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def login_with_saved_credentials(self, _timeout):
            return LoginState.AUTHENTICATED

    class FakeAdapter:
        def __init__(self, _page):
            pass

        def capture_direct(self, _base_url, _workitem_id, **kwargs):
            observed.update(kwargs)
            return SimpleNamespace(
                attachments=(), related_containers=(), capture_issues=(),
            )

    monkeypatch.setattr("oa_knowledge.collector.browser.BrowserSession", FakeBrowser)
    monkeypatch.setattr("oa_knowledge.collector.detail.CollaborationDetailAdapter", FakeAdapter)
    worker = OperationWorker(settings, config_path=config_file)
    try:
        assert worker.run_once() is True
    finally:
        worker.close()

    assert observed["total_timeout_seconds"] == 73
    assert observed["download_timeout_seconds"] == 17
    with Session(engine) as session:
        assert session.get(OnlineAuditRun, run_id).status == "completed"


def test_online_audit_rejects_partial_evidence_after_capture_timeout(config_file: Path, monkeypatch) -> None:
    """Timed-out capture bytes cannot be treated as complete online evidence."""
    from oa_knowledge.collector import LoginState

    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        session.add(OAManifestItem(
            oa_item_key="done:timed-out-audit", workitem_id_text="timed-out-audit",
            title="合成已办", list_page=1, processing_status="downloaded",
        ))
        session.commit()
    run_id = start_audit(settings)["run_id"]

    class FakeBrowser:
        page = MagicMock()
        base_url = "http://oa.invalid"
        def __init__(self, *_args, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def login_with_saved_credentials(self, _timeout): return LoginState.AUTHENTICATED

    class FakeAdapter:
        def __init__(self, _page): pass
        def capture_direct(self, *_args, **_kwargs):
            return SimpleNamespace(
                attachments=(), related_containers=(),
                capture_issues=({"kind": "capture_timeout", "stage": "attachments"},),
            )

    monkeypatch.setattr("oa_knowledge.collector.browser.BrowserSession", FakeBrowser)
    monkeypatch.setattr("oa_knowledge.collector.detail.CollaborationDetailAdapter", FakeAdapter)
    monkeypatch.setattr("oa_knowledge.web.worker.ONLINE_AUDIT_YIELD", timedelta(0))
    worker = OperationWorker(settings, config_path=config_file)
    try:
        assert worker.run_once() is True
        assert worker.run_once() is True
    finally:
        worker.close()

    with Session(engine) as session:
        run = session.get(OnlineAuditRun, run_id)
        item = session.scalar(select(OnlineAuditItem).where(OnlineAuditItem.run_id == run_id))
        assert run.status == "completed"
        assert run.access_failed_items == 1
        assert item.status == "access_failed"


def test_verified_archive_migration_waits_for_completed_audit(config_file: Path) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = OAItem(
            oa_item_key="done:synthetic-migration", source_channel="done",
            title="合成已办", archive_relpath="raw/done/unknown/synthetic",
        )
        session.add(item)
        session.add(OnlineAuditRun(status="running", total_items=1))
        session.commit()
    assert ensure_verified_archive_migration(engine) is None


def test_verified_archive_migration_waits_for_fresh_full_manifest(config_file: Path) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = OAItem(
            oa_item_key="done:stale-manifest", source_channel="done",
            title="stale-manifest",
            archive_relpath="raw/done/unknown/stale-manifest",
        )
        session.add(item)
        session.add(OAManifestItem(
            oa_item_key=item.oa_item_key, workitem_id_text="stale-manifest",
            title="stale-manifest", list_page=1,
            processing_status="downloaded",
        ))
        audit = OnlineAuditRun(
            status="completed", total_items=1, completed_items=1,
            started_at=datetime.now(timezone.utc),
        )
        session.add(audit); session.flush()
        session.add(OnlineAuditItem(
            run_id=audit.id, oa_item_key=item.oa_item_key,
            title="stale-manifest", status="matched",
            comparison_reason="exact_match",
        ))
        old = datetime(2000, 1, 1, tzinfo=timezone.utc)
        session.add(OAManifestSync(
            oa_total_count=1, local_manifest_count=1,
            pages_scanned=1, source_total_pages=1,
            status="manifest_complete", started_at=old, finished_at=old,
        ))
        session.commit()

    assert ensure_verified_archive_migration(engine) is None
    with Session(engine) as session:
        assert session.scalar(select(OperationJob.id).where(
            OperationJob.job_type == "verified_archive_migration",
        )) is None


def test_verified_archive_migration_waits_for_audit_supplement(config_file: Path) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = OAItem(
            oa_item_key="done:supplement", source_channel="done", title="supplement",
            archive_relpath="raw/done/unknown/supplement",
        )
        session.add(item)
        audit = OnlineAuditRun(status="completed", total_items=1, completed_items=1)
        session.add(audit); session.flush()
        session.add(OnlineAuditItem(
            run_id=audit.id, oa_item_key=item.oa_item_key, title="supplement",
            status="matched", comparison_reason="exact_match",
        ))
        session.add(PipelineTask(
            queue_name="realtime_done", priority=10,
            logical_item_key=item.oa_item_key,
            stage="done_capture_and_archive",
            idempotency_key=f"online-audit:{audit.id}:synthetic:supplement-v1",
            status="queued",
        ))
        session.commit()

    assert ensure_verified_archive_migration(engine) is None


def test_verified_archive_migration_reopens_audit_for_new_manifest(config_file: Path) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = OAItem(
            oa_item_key="done:audited", source_channel="done", title="audited",
            archive_relpath="raw/done/unknown/audited",
        )
        session.add(item)
        job = OperationJob(
            job_key="completed-audit-with-late-manifest", job_type="online_audit",
            status="completed", idempotency_key="completed-audit-with-late-manifest",
        )
        session.add(job); session.flush()
        audit = OnlineAuditRun(
            job_id=job.id, status="completed", total_items=1, completed_items=1,
        )
        session.add(audit); session.flush()
        session.add(OnlineAuditItem(
            run_id=audit.id, oa_item_key=item.oa_item_key, title="audited",
            status="matched", comparison_reason="exact_match",
        ))
        session.add(OAManifestItem(
            oa_item_key="done:late-manifest", workitem_id_text="late",
            title="late", list_page=9, processing_status="pending_download",
        ))
        audit_id = audit.id
        session.commit()

    assert ensure_verified_archive_migration(engine) is None
    with Session(engine) as session:
        audit = session.get(OnlineAuditRun, audit_id)
        assert audit.status == "queued"
        assert audit.total_items == 2
        assert session.get(OperationJob, audit.job_id).status == "queued"
        assert session.scalar(select(func.count()).select_from(OnlineAuditItem).where(
            OnlineAuditItem.run_id == audit_id,
            OnlineAuditItem.status == "pending",
        )) == 1


def test_archive_migration_pauses_and_resumes_when_audit_reopens(config_file: Path) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = OAItem(
            oa_item_key="done:migration-resume", source_channel="done",
            title="migration-resume",
            archive_relpath="raw/done/unknown/migration-resume",
        )
        session.add(item)
        audit_job = OperationJob(
            job_key="reopened-audit-job", job_type="online_audit",
            status="queued", idempotency_key="reopened-audit-job",
        )
        session.add(audit_job); session.flush()
        audit = OnlineAuditRun(
            job_id=audit_job.id, status="queued", total_items=1,
            completed_items=1,
        )
        session.add(audit); session.flush()
        session.add(OnlineAuditItem(
            run_id=audit.id, oa_item_key=item.oa_item_key,
            title="migration-resume", status="matched",
            comparison_reason="exact_match",
        ))
        session.add(OAManifestItem(
            oa_item_key=item.oa_item_key, workitem_id_text="migration-resume",
            title="migration-resume", list_page=1,
            processing_status="downloaded",
        ))
        _record_full_manifest_sync(session, 1)
        migration = OperationJob(
            job_key=f"archive-migration-{audit.id}",
            job_type="verified_archive_migration", status="queued",
            idempotency_key=f"archive-migration:{audit.id}:verified-archive-path-v1",
            parameters_json=json.dumps({
                "audit_run_id": audit.id,
                "migration_version": "verified-archive-path-v1",
                "processed": 0, "migrated": 0, "failed": 0,
                "failed_item_ids": [],
            }),
        )
        session.add(migration); session.flush()
        audit_id, migration_id = audit.id, migration.id
        session.commit()

    worker = OperationWorker(settings, config_path=config_file)
    try:
        worker._execute_verified_archive_migration(migration_id)
    finally:
        worker.close()
    with Session(engine) as session:
        migration = session.get(OperationJob, migration_id)
        assert migration.status == "paused"
        assert migration.last_error_code == "WAITING_FOR_ONLINE_AUDIT"
        audit = session.get(OnlineAuditRun, audit_id)
        audit.status = "completed"
        session.get(OperationJob, audit.job_id).status = "completed"
        session.commit()

    assert ensure_verified_archive_migration(engine) == migration_id
    with Session(engine) as session:
        migration = session.get(OperationJob, migration_id)
        assert migration.status == "queued"
        assert migration.last_error_code is None


def test_worker_migrates_only_online_verified_legacy_archives(config_file: Path) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    safe_rel = "raw/done/unknown/safe"
    review_rel = "raw/done/unknown/review"
    (settings.data_root / safe_rel).mkdir(parents=True)
    (settings.data_root / safe_rel / "source.txt").write_bytes(b"safe immutable source")
    (settings.data_root / review_rel).mkdir(parents=True)
    (settings.data_root / review_rel / "source.txt").write_bytes(b"review immutable source")
    with Session(engine) as session:
        safe = OAItem(
            oa_item_key="done:safe", source_channel="done", title="safe",
            archive_relpath=safe_rel,
        )
        review = OAItem(
            oa_item_key="done:review", source_channel="done", title="review",
            archive_relpath=review_rel,
        )
        session.add_all((safe, review)); session.flush()
        session.add_all((
            ArchivedFile(
                oa_item_id=safe.id, original_name="source.txt", attachment_key="safe",
                file_role="direct_attachment", source_container_key="root", depth=1,
                local_relpath=f"{safe_rel}/source.txt", download_status="verified",
            ),
            ArchivedFile(
                oa_item_id=review.id, original_name="source.txt", attachment_key="review",
                file_role="direct_attachment", source_container_key="root", depth=1,
                local_relpath=f"{review_rel}/source.txt", download_status="verified",
            ),
        ))
        session.add_all((
            OAManifestItem(
                oa_item_key=safe.oa_item_key, workitem_id_text="safe",
                title="safe", list_page=1, processing_status="downloaded",
            ),
            OAManifestItem(
                oa_item_key=review.oa_item_key, workitem_id_text="review",
                title="review", list_page=1, processing_status="downloaded",
            ),
        ))
        audit = OnlineAuditRun(status="completed", total_items=2, completed_items=2)
        session.add(audit); session.flush()
        session.add_all((
            OnlineAuditItem(
                run_id=audit.id, oa_item_key=safe.oa_item_key, title="safe",
                status="matched", comparison_reason="exact_match",
                depth_limit_reached=False,
            ),
            OnlineAuditItem(
                run_id=audit.id, oa_item_key=review.oa_item_key, title="review",
                status="historical_retained", comparison_reason="inventory_changed",
                depth_limit_reached=False,
            ),
        ))
        _record_full_manifest_sync(session, 2)
        session.add(MarkdownQueueControl(id=1, paused=True))
        session.commit()

    safe_task_id = ProductionQueue(engine).enqueue(
        "historical_done_backfill", "done:safe", "attachment_inventory",
        "history:done:safe:knowledge-v2",
    )
    review_task_id = ProductionQueue(engine).enqueue(
        "historical_done_backfill", "done:review", "attachment_inventory",
        "history:done:review:knowledge-v2",
    )

    worker = OperationWorker(settings, config_path=config_file)
    try:
        assert worker.run_once() is True
    finally:
        worker.close()

    with Session(engine) as session:
        safe = session.scalar(select(OAItem).where(OAItem.oa_item_key == "done:safe"))
        review = session.scalar(select(OAItem).where(OAItem.oa_item_key == "done:review"))
        job = session.scalar(select(OperationJob).where(
            OperationJob.job_type == "verified_archive_migration",
        ))
        assert safe.archive_relpath == "originals/unknown/unknown_safe"
        assert review.archive_relpath == review_rel
        assert job.status == "completed"
        parameters = json.loads(job.parameters_json)
        assert parameters["review_required"] == 1
        assert parameters["historical_released_tasks"] == 0
        assert parameters["historical_review_tasks"] == 1
        assert session.get(PipelineTask, safe_task_id).status == "queued"
        review_task = session.get(PipelineTask, review_task_id)
        assert review_task.status == "failed"
        assert review_task.error_code == "ONLINE_AUDIT_REVIEW_REQUIRED"
        assert review_task.recoverable is False
        assert session.get(MarkdownQueueControl, 1).paused is True
    assert (settings.data_root / safe.archive_relpath / "source.txt").read_bytes() == b"safe immutable source"
    assert (settings.data_root / review_rel / "source.txt").read_bytes() == b"review immutable source"


def test_verified_archive_migration_waits_for_active_markdown_reader(config_file: Path) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    legacy_rel = "raw/done/unknown/active-reader"
    source_path = settings.data_root / legacy_rel / "source.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"active reader source")
    with Session(engine) as session:
        item = OAItem(
            oa_item_key="done:active-reader", source_channel="done", title="active-reader",
            archive_relpath=legacy_rel,
        )
        session.add(item); session.flush()
        source = ArchivedFile(
            oa_item_id=item.id, original_name="source.txt", attachment_key="active",
            file_role="direct_attachment", source_container_key="root", depth=1,
            local_relpath=f"{legacy_rel}/source.txt", download_status="verified",
        )
        session.add(source); session.flush()
        session.add(MarkdownTask(
            source_file_id=source.id, schema_version="synthetic-v1", status="running",
            campaign="standard", lease_owner="markdown-worker-test",
        ))
        audit = OnlineAuditRun(status="completed", total_items=1, completed_items=1)
        session.add(audit); session.flush()
        session.add(OnlineAuditItem(
            run_id=audit.id, oa_item_key=item.oa_item_key, title="active-reader",
            status="matched", comparison_reason="exact_match",
            depth_limit_reached=False,
        ))
        session.add(OAManifestItem(
            oa_item_key=item.oa_item_key, workitem_id_text="active-reader",
            title="active-reader", list_page=1,
            processing_status="downloaded",
        ))
        _record_full_manifest_sync(session, 1)
        session.commit()

    worker = OperationWorker(settings, config_path=config_file)
    try:
        assert worker.run_once() is True
    finally:
        worker.close()

    with Session(engine) as session:
        job = session.scalar(select(OperationJob).where(
            OperationJob.job_type == "verified_archive_migration",
        ))
        assert job.status == "queued"
        assert session.get(MarkdownQueueControl, 1).paused is True
        item = session.scalar(select(OAItem).where(OAItem.oa_item_key == "done:active-reader"))
        assert item.archive_relpath == legacy_rel
    assert source_path.read_bytes() == b"active reader source"


def test_worker_claims_one_job_and_records_unsupported_type(config_file: Path) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        session.add(OperationJob(
            job_key="unsupported-1", job_type="unsupported",
            idempotency_key="unsupported-idem-1", status="queued",
        ))
        session.commit()
    worker = OperationWorker(settings, config_path=config_file)
    try:
        assert worker.run_once() is True
        assert worker.run_once() is False
    finally:
        worker.close()
    with Session(engine) as session:
        job = session.query(OperationJob).filter_by(job_key="unsupported-1").one()
        assert job.status == "failed"
        assert job.last_error_code == "unsupported_job_type"
        assert job.lease_owner is None
        assert [event.event_type for event in job.events] == ["finished"]


def test_backfill_campaign_quarantines_failed_item_and_continues(config_file: Path, monkeypatch) -> None:
    settings = load_settings(config_file)
    (settings.data_root / "originals").mkdir(parents=True)
    (settings.data_root / "markdown").mkdir()
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        batch = CollectionBatch(
            batch_key="backfill-failed", plan_hash="f" * 64, source_channel="done",
            planned_limit=1, discovered_count=1, failed_count=1, status="paused",
            frozen_at=datetime.now(timezone.utc),
            notes="backfill:v1;granularity=month;range=2019-01-01:2026-01-01",
        )
        batch.items.append(BatchItem(
            oa_item_key="failed-1", workitem_id_text="1", title="附件很多的事项",
            ordinal=1, archive_status="collect_failed", retry_count=2, last_error="TimeoutError: budget",
        ))
        session.add(batch)
        session.add(OperationJob(
            job_key="campaign-1", job_type="backfill_campaign", status="queued",
            idempotency_key="campaign-idem-1",
            parameters_json='{"from_date":"2019-01-01","to_date":"2026-01-01","chunk_size":20,"time_budget_seconds":1800}',
        ))
        session.commit()
    worker = OperationWorker(settings, config_path=config_file)
    def fake_cli(arguments, _timeout):
        if arguments[:2] == ["batch", "validate"]:
            return 0, {"reconciled": True, "source_match": True, "reviewed": 1}
        return 0, {"status": "complete", "cursor": "2019-01-01"}
    monkeypatch.setattr(worker, "_run_cli", fake_cli)
    try:
        assert worker.run_once() is True
    finally:
        worker.close()
    with Session(engine) as session:
        job = session.query(OperationJob).filter_by(job_key="campaign-1").one()
        review = session.query(ReviewEntry).filter_by(kind="collection_issue").one()
        assert job.status == "completed"
        assert job.last_error_code is None
        assert "附件很多的事项" in review.details_json
        item = session.query(BatchItem).filter_by(oa_item_key="failed-1").one()
        assert item.archive_status == "review_required"
        item.archive_status = "archived"
        OperationWorker._resolve_recovered_collection_issues(session, item.batch_id)
        session.commit()
        assert review.status == "resolved"


def test_worker_immediately_recovers_dead_owner_lease(config_file: Path) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        session.add(OperationJob(
            job_key="dead-owner", job_type="unsupported", status="running",
            idempotency_key="dead-owner-idem", lease_owner="worker-999999999",
        ))
        session.commit()
    worker = OperationWorker(settings, config_path=config_file)
    try:
        assert worker.recover_expired() == 1
    finally:
        worker.close()
    with Session(engine) as session:
        job = session.query(OperationJob).filter_by(job_key="dead-owner").one()
        assert job.status == "queued"
        assert job.lease_owner is None
        assert job.last_error_code == "recovered_expired_lease"


def test_worker_records_unexpected_dispatch_exception(config_file: Path, monkeypatch) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        session.add(OperationJob(
            job_key="campaign-crash", job_type="backfill_campaign", status="queued",
            idempotency_key="campaign-crash-idem", parameters_json="{}",
        ))
        session.commit()
    worker = OperationWorker(settings, config_path=config_file)
    monkeypatch.setattr(worker, "_execute_backfill_campaign", lambda _job_id: (_ for _ in ()).throw(RuntimeError("secret detail")))
    try:
        assert worker.run_once() is True
    finally:
        worker.close()
    with Session(engine) as session:
        job = session.query(OperationJob).filter_by(job_key="campaign-crash").one()
        assert job.status == "failed"
        assert job.last_error_code == "worker_exception_RuntimeError"
        assert "secret detail" not in job.events[-1].details_json


def test_worker_records_visible_retry_without_stopping_campaign(config_file: Path) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        job = OperationJob(
            job_key="campaign-retry", job_type="backfill_campaign", status="running",
            idempotency_key="campaign-retry-idem", parameters_json="{}",
        )
        session.add(job); session.commit(); job_id = job.id
    worker = OperationWorker(settings, config_path=config_file)
    try:
        worker._record_runner_retry(job_id, 1, {"reason": "cli_exit_1", "stderr_sha256": "a" * 64}, 1)
    finally:
        worker.close()
    with Session(engine) as session:
        job = session.get(OperationJob, job_id)
        assert job.status == "running"
        assert job.last_error_code == "runner_retry_1_exit_1"
        assert job.events[-1].event_type == "runner_retry"


def test_worker_consumes_production_queue_when_operation_queue_is_empty(config_file: Path, monkeypatch) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    ProductionQueue(engine).enqueue("realtime_done", "done-1", "attachment_inventory", "production-1")
    worker = OperationWorker(settings, config_path=config_file)
    handled = []
    monkeypatch.setattr(worker, "_execute_pipeline_task", lambda task: handled.append(task.id))
    try:
        assert worker.run_once() is True
    finally:
        worker.close()
    assert len(handled) == 1


def test_recent_online_audit_yield_allows_realtime_pipeline_work(config_file: Path, monkeypatch) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        session.add(OperationJob(
            job_key="online-yield", job_type="online_audit", status="queued",
            idempotency_key="online-yield-idem", heartbeat_at=datetime.now(timezone.utc),
        ))
        session.commit()
    task_id = ProductionQueue(engine).enqueue(
        "realtime_pending", "pending-1", "detail_sync", "realtime-during-audit",
    )
    worker = OperationWorker(settings, config_path=config_file)
    handled = []
    monkeypatch.setattr(worker, "_execute_pipeline_task", lambda task: handled.append(task.id))
    try:
        assert worker.run_once() is True
    finally:
        worker.close()
    assert handled == [task_id]


def test_recent_online_audit_yield_does_not_start_historical_rebuild(config_file: Path, monkeypatch) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        session.add(OperationJob(
            job_key="online-yield-before-history", job_type="online_audit", status="queued",
            idempotency_key="online-yield-before-history-idem", heartbeat_at=datetime.now(timezone.utc),
        ))
        session.commit()
    ProductionQueue(engine).enqueue(
        "historical_done_backfill", "done-1", "attachment_inventory", "history-waits-for-audit",
    )
    worker = OperationWorker(settings, config_path=config_file)
    handled = []
    monkeypatch.setattr(worker, "_execute_pipeline_task", lambda task: handled.append(task.id))
    try:
        assert worker.run_once() is False
    finally:
        worker.close()
    assert handled == []


def test_ready_realtime_task_preempts_online_audit_even_after_cooldown(config_file: Path, monkeypatch) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        session.add(OperationJob(
            job_key="online-stale-yield", job_type="online_audit", status="queued",
            idempotency_key="online-stale-yield-idem",
            heartbeat_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
        ))
        session.commit()
    task_id = ProductionQueue(engine).enqueue(
        "realtime_done", "done-1", "attachment_inventory", "realtime-preempts-audit",
    )
    worker = OperationWorker(settings, config_path=config_file)
    handled = []
    monkeypatch.setattr(worker, "_execute_pipeline_task", lambda task: handled.append(task.id))
    try:
        assert worker.run_once() is True
    finally:
        worker.close()
    assert handled == [task_id]


def test_ready_realtime_task_preempts_filesystem_governance_job(config_file: Path, monkeypatch) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        session.add(OperationJob(
            job_key="integrity-audit-waits", job_type="data_governance", status="queued",
            idempotency_key="integrity-audit-waits-idem",
            parameters_json='{"action":"integrity_audit"}',
        ))
        session.commit()
    task_id = ProductionQueue(engine).enqueue(
        "realtime_pending", "pending-1", "pending_summary", "realtime-preempts-governance",
    )
    worker = OperationWorker(settings, config_path=config_file)
    handled = []
    monkeypatch.setattr(worker, "_execute_pipeline_task", lambda task: handled.append(task.id))
    try:
        assert worker.run_once() is True
    finally:
        worker.close()
    assert handled == [task_id]


def test_ready_realtime_task_preempts_archive_migration(config_file: Path, monkeypatch) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        session.add(OperationJob(
            job_key="migration-waits-for-realtime",
            job_type="verified_archive_migration", status="queued",
            idempotency_key="migration-waits-for-realtime-idem",
            parameters_json='{"audit_run_id":999}',
        ))
        session.commit()
    task_id = ProductionQueue(engine).enqueue(
        "realtime_pending", "pending-1", "pending_summary",
        "realtime-preempts-migration",
    )
    worker = OperationWorker(settings, config_path=config_file)
    handled = []
    monkeypatch.setattr(worker, "_execute_pipeline_task", lambda task: handled.append(task.id))
    try:
        assert worker.run_once() is True
    finally:
        worker.close()
    assert handled == [task_id]


def test_unsupported_conversion_is_nonfatal_for_an_individual_file() -> None:
    assert OperationWorker._nonfatal_parse_error(type("UnsupportedFormatException", (Exception,), {})())
    assert OperationWorker._nonfatal_parse_error(type("FileConversionException", (Exception,), {})())
    assert not OperationWorker._nonfatal_parse_error(RuntimeError("parser service down"))


def test_nonfatal_pending_parse_error_marks_job_skipped(config_file: Path) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = OAItem(oa_item_key="pending:synthetic", source_channel="pending", title="Synthetic")
        session.add(item); session.flush()
        file = ArchivedFile(
            oa_item_id=item.id, original_name="synthetic.doc", attachment_key="synthetic",
            file_role="direct_attachment", source_container_key="synthetic", depth=1,
            local_relpath="raw/synthetic.doc", download_status="verified",
        )
        session.add(file); session.flush()
        job = ParseJob(file_id=file.id, engine="markitdown", engine_version="test", config_hash="", status="queued")
        session.add(job); session.commit(); job_id = job.id

    worker = OperationWorker(settings, config_path=config_file)
    try:
        handled = worker._handle_nonfatal_parse_error(
            job_id, type("FileConversionException", (Exception,), {})(),
        )
    finally:
        worker.close()

    with Session(engine) as session:
        job = session.get(ParseJob, job_id)
        assert handled is True
        assert job.status == "skipped"
        assert job.error_code == "unsupported_format"


def test_historical_sources_keep_attachments_and_only_canonical_snapshots() -> None:
    from types import SimpleNamespace

    files = [
        SimpleNamespace(id=1, file_role="body_snapshot", original_name="body.html"),
        SimpleNamespace(id=2, file_role="body_snapshot", original_name="body-01-downloadFileFrame.html"),
        SimpleNamespace(id=3, file_role="workflow_snapshot", original_name="workflow.json"),
        SimpleNamespace(id=4, file_role="workflow_snapshot", original_name="workflow-03-iframeright.html"),
        SimpleNamespace(id=5, file_role="direct_attachment", original_name="合同.pdf"),
        SimpleNamespace(id=6, file_role="official_attachment", original_name="批复.pdf"),
        SimpleNamespace(id=7, file_role="official_body", original_name="正文.pdf"),
        SimpleNamespace(id=8, file_role="associated_document", original_name="关联文件.pdf"),
        SimpleNamespace(id=9, file_role="opinion_attachment", original_name="意见.pdf"),
    ]

    selected = OperationWorker._historical_source_files(files)

    assert [file.id for file in selected] == [5, 6, 7, 8, 9]


def test_done_parse_advances_to_source_publish_before_curation(config_file: Path) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    queue = ProductionQueue(engine)
    with Session(engine) as session:
        item = OAItem(oa_item_key="done:source-stage", source_channel="done", title="Synthetic")
        session.add(item); session.flush()
        source = ArchivedFile(
            oa_item_id=item.id, original_name="source.pdf", attachment_key="source-stage",
            file_role="direct_attachment", source_container_key="root", depth=1,
            local_relpath="originals/unknown/source.pdf", download_status="verified",
        )
        session.add(source); session.flush()
        session.add(ParseJob(
            file_id=source.id, engine="synthetic", engine_version="1",
            config_hash="c" * 64, status="completed",
        ))
        session.commit()
        source_id = source.id
    task_id = queue.enqueue("realtime_done", "done:source-stage", "parse", "source-stage-task")
    task = queue.claim("worker-test")
    worker = OperationWorker(settings, config_path=config_file); worker.owner = "worker-test"
    try:
        worker._pipeline_parse(task)
    finally:
        worker.close()
    with Session(engine) as session:
        row = session.get(PipelineTask, task_id)
        assert row.status == "queued"
        assert row.stage == "source_publish"


def test_source_publish_advances_to_classify_and_missing_artifact_stops(config_file: Path, monkeypatch) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    queue = ProductionQueue(engine)
    with Session(engine) as session:
        item = OAItem(
            oa_item_key="done:publish",
            source_channel="done",
            title="Synthetic",
            archive_relpath="originals/unknown/unknown_publish",
        )
        session.add(item); session.flush()
        source = ArchivedFile(
            oa_item_id=item.id, original_name="source.pdf", attachment_key="publish",
            file_role="direct_attachment", source_container_key="root", depth=1,
            local_relpath="originals/unknown/source.pdf", download_status="verified",
        )
        session.add(source); session.flush()
        content = ContentObject(sha256="a" * 64, size_bytes=1)
        session.add(content); session.flush()
        source.content_object_id = content.id
        job = ParseJob(
            file_id=source.id, engine="synthetic", engine_version="1",
            config_hash="c" * 64, status="completed",
        )
        session.add(job); session.flush()
        artifact = ParseArtifact(
            parse_job_id=job.id, content_object_id=content.id, engine="synthetic",
            engine_version="1", output_relpath="synthetic/source.md",
            source_sha256=content.sha256, product_sha256="b" * 64,
            config_hash="c" * 64, lifecycle_status="valid",
        )
        session.add(artifact); session.flush()
        content.active_parse_artifact_id = artifact.id
        audit = OnlineAuditRun(status="completed", total_items=1, completed_items=1)
        session.add(audit); session.flush()
        session.add(OnlineAuditItem(
            run_id=audit.id,
            oa_item_key=item.oa_item_key,
            title=item.title,
            status="matched",
            comparison_reason="exact_match",
            depth_limit_reached=False,
        ))
        session.commit(); source_id = source.id; parse_job_id = job.id
    task_id = queue.enqueue("historical_done_backfill", "done:publish", "source_publish", "publish-task")
    task = queue.claim("worker-test")
    called: list[int] = []
    monkeypatch.setattr(
        "oa_knowledge.source_markdown.service.publish_active_artifact",
        lambda _session, _settings, file_id: called.append(file_id),
    )
    worker = OperationWorker(settings, config_path=config_file); worker.owner = "worker-test"
    try:
        worker._pipeline_source_publish(task)
    finally:
        worker.close()
    with Session(engine) as session:
        row = session.get(PipelineTask, task_id)
        assert called == []
        assert row.stage == "classify" and row.status == "queued"

    source_path = settings.data_root / "originals/unknown/source.pdf"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"%PDF synthetic")
    with Session(engine) as session:
        artifact = session.scalar(select(ParseArtifact).where(ParseArtifact.parse_job_id == parse_job_id))
        assert artifact is not None
        artifact.lifecycle_status = "superseded"
        session.commit()

    missing_id = queue.enqueue("historical_done_backfill", "done:publish", "source_publish", "publish-missing")
    missing = queue.claim("worker-test")
    monkeypatch.setattr(
        "oa_knowledge.source_markdown.service.publish_active_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("valid parse artifact unavailable")),
    )
    worker = OperationWorker(settings, config_path=config_file); worker.owner = "worker-test"
    try:
        worker._pipeline_source_publish(missing)
    finally:
        worker.close()
    with Session(engine) as session:
        row = session.get(PipelineTask, missing_id)
        assert row.status == "queued"
        assert row.stage == "attachment_inventory"
        assert row.error_code is None


def test_source_publish_records_unsupported_source_in_item_index_without_review(
    config_file: Path,
) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    source_path = settings.data_root / "originals/unknown/synthetic/source.wps"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"synthetic unsupported source")
    with Session(engine) as session:
        item = OAItem(oa_item_key="done:unsupported", source_channel="done", title="Synthetic")
        session.add(item)
        session.flush()
        session.add(ArchivedFile(
            oa_item_id=item.id,
            original_name="source.wps",
            attachment_key="unsupported",
            file_role="direct_attachment",
            source_container_key="root",
            depth=1,
            local_relpath="originals/unknown/synthetic/source.wps",
            download_status="verified",
        ))
        session.commit()
    queue = ProductionQueue(engine)
    task_id = queue.enqueue(
        "realtime_done", "done:unsupported", "source_publish", "unsupported-source",
    )
    task = queue.claim("worker-test")
    worker = OperationWorker(settings, config_path=config_file)
    worker.owner = "worker-test"
    try:
        worker._pipeline_source_publish(task)
    finally:
        worker.close()

    with Session(engine) as session:
        row = session.get(PipelineTask, task_id)
        assert row.status == "queued"
        assert row.stage == "classify"
        assert session.query(ReviewEntry).filter_by(kind="source_markdown_incomplete").count() == 0


def test_source_publish_records_parser_skipped_source_without_review(config_file: Path) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    source_path = settings.data_root / "originals/unknown/synthetic/source.doc"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"synthetic legacy document")
    with Session(engine) as session:
        item = OAItem(oa_item_key="done:skipped", source_channel="done", title="Synthetic")
        session.add(item)
        session.flush()
        source = ArchivedFile(
            oa_item_id=item.id, original_name="source.doc", attachment_key="skipped",
            file_role="direct_attachment", source_container_key="root", depth=1,
            local_relpath="originals/unknown/synthetic/source.doc",
            download_status="verified",
        )
        session.add(source)
        session.flush()
        session.add(ParseJob(
            file_id=source.id, engine="markitdown", engine_version="test",
            config_hash="", status="skipped", error_code="unsupported_format",
        ))
        session.commit()
        source_id = source.id
    queue = ProductionQueue(engine)
    task_id = queue.enqueue(
        "realtime_done", "done:skipped", "source_publish", "skipped-source",
    )
    task = queue.claim("worker-test")
    worker = OperationWorker(settings, config_path=config_file)
    worker.owner = "worker-test"
    try:
        worker._pipeline_source_publish(task)
    finally:
        worker.close()

    with Session(engine) as session:
        row = session.get(PipelineTask, task_id)
        assert row.status == "queued"
        assert row.stage == "classify"
        assert session.query(ReviewEntry).filter_by(
            kind="source_markdown_incomplete", file_id=source_id,
        ).count() == 0


def test_source_publish_fails_rejected_quality_artifact_without_review(config_file: Path) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    source_path = settings.data_root / "originals/unknown/synthetic/source.docx"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"synthetic low-quality document")
    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    with Session(engine) as session:
        item = OAItem(oa_item_key="done:rejected", source_channel="done", title="Synthetic")
        session.add(item)
        session.flush()
        content = ContentObject(sha256=digest, size_bytes=source_path.stat().st_size)
        session.add(content)
        session.flush()
        source = ArchivedFile(
            oa_item_id=item.id, original_name="source.docx", attachment_key="rejected",
            file_role="direct_attachment", source_container_key="root", depth=1,
            local_relpath="originals/unknown/synthetic/source.docx",
            download_status="verified", sha256=digest, content_object_id=content.id,
        )
        session.add(source)
        session.flush()
        job = ParseJob(
            file_id=source.id, engine="markitdown", engine_version="test",
            config_hash="c" * 64, status="completed",
        )
        session.add(job)
        session.flush()
        session.add(ParseArtifact(
            parse_job_id=job.id, content_object_id=content.id, engine="markitdown",
            engine_version="test", output_relpath="synthetic/rejected.md",
            source_sha256=digest, product_sha256="d" * 64,
            config_hash="c" * 64, quality_score=0.2,
            lifecycle_status="rejected",
        ))
        session.commit()
        source_id = source.id
    queue = ProductionQueue(engine)
    task_id = queue.enqueue(
        "realtime_done", "done:rejected", "source_publish", "rejected-source",
    )
    task = queue.claim("worker-test")
    worker = OperationWorker(settings, config_path=config_file)
    worker.owner = "worker-test"
    try:
        worker._pipeline_source_publish(task)
    finally:
        worker.close()

    with Session(engine) as session:
        row = session.get(PipelineTask, task_id)
        assert row.status == "failed"
        assert row.error_code == "PARSE_QUALITY_REJECTED"
        assert session.query(ReviewEntry).filter_by(
            kind="source_markdown_incomplete", file_id=source_id,
        ).count() == 0


def test_source_publish_keeps_unsupported_files_out_of_valid_artifact_publication(
    config_file: Path, monkeypatch,
) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    root = settings.data_root / "originals/unknown/synthetic"
    root.mkdir(parents=True)
    (root / "first.docx").write_bytes(b"synthetic eligible source")
    (root / "second.wps").write_bytes(b"synthetic unsupported source")
    with Session(engine) as session:
        item = OAItem(oa_item_key="done:preflight", source_channel="done", title="Synthetic")
        session.add(item)
        session.flush()
        first = ArchivedFile(
            oa_item_id=item.id, original_name="first.docx", attachment_key="first",
            file_role="direct_attachment", source_container_key="root", depth=1,
            local_relpath="originals/unknown/synthetic/first.docx", download_status="verified",
        )
        second = ArchivedFile(
            oa_item_id=item.id, original_name="second.wps", attachment_key="second",
            file_role="official_attachment", source_container_key="root", depth=1,
            local_relpath="originals/unknown/synthetic/second.wps", download_status="verified",
        )
        session.add_all([first, second]); session.flush()
        content = ContentObject(sha256=hashlib.sha256(b"synthetic eligible source").hexdigest(), size_bytes=25)
        session.add(content); session.flush()
        first.content_object_id = content.id
        job = ParseJob(
            file_id=first.id, engine="synthetic", engine_version="1",
            config_hash="c" * 64, status="completed",
        )
        session.add(job); session.flush()
        artifact = ParseArtifact(
            parse_job_id=job.id, content_object_id=content.id, engine="synthetic",
            engine_version="1", output_relpath="synthetic/first.md",
            source_sha256=content.sha256, product_sha256="d" * 64,
            config_hash="c" * 64, lifecycle_status="valid",
        )
        session.add(artifact); session.flush()
        content.active_parse_artifact_id = artifact.id
        session.commit()
        first_id = first.id
        second_id = second.id
    calls: list[int] = []

    def publish(_session, _settings, file_id: int):
        calls.append(file_id)
        assert file_id != second_id

    monkeypatch.setattr("oa_knowledge.source_markdown.service.publish_active_artifact", publish)
    queue = ProductionQueue(engine)
    task_id = queue.enqueue(
        "realtime_done", "done:preflight", "source_publish", "preflight-source",
    )
    task = queue.claim("worker-test")
    worker = OperationWorker(settings, config_path=config_file)
    worker.owner = "worker-test"
    try:
        worker._pipeline_source_publish(task)
    finally:
        worker.close()

    with Session(engine) as session:
        assert calls == []
        row = session.get(PipelineTask, task_id)
        assert row.status == "queued"
        assert row.stage == "classify"
        assert session.query(ReviewEntry).filter_by(kind="source_markdown_incomplete").count() == 0


def test_pending_summary_with_notification_requested_advances_to_notify_stage(config_file: Path, monkeypatch) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    queue = ProductionQueue(engine)
    task_id = queue.enqueue(
        "realtime_pending", "1", "pending_summary", "pending-summary-notify",
        payload={"notify": True},
    )
    task = queue.claim("worker-test")
    worker = OperationWorker(settings, config_path=config_file)
    worker.owner = "worker-test"
    monkeypatch.setattr("oa_knowledge.pending_summary.summarize_pending", lambda *_args, **_kwargs: object())
    try:
        worker._pipeline_pending_summary(task)
    finally:
        worker.close()

    with Session(engine) as session:
        row = session.get(PipelineTask, task_id)
        # notify=True advances into the delivery stage rather than completing.
        assert row.status == "queued"
        assert row.stage == "notify_feishu"
        assert row.error_code is None


def test_pending_summary_without_notification_completes(config_file: Path, monkeypatch) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    queue = ProductionQueue(engine)
    task_id = queue.enqueue(
        "realtime_pending", "1", "pending_summary", "pending-summary-no-notify",
        payload={"notify": False},
    )
    task = queue.claim("worker-test")
    worker = OperationWorker(settings, config_path=config_file)
    worker.owner = "worker-test"
    monkeypatch.setattr("oa_knowledge.pending_summary.summarize_pending", lambda *_args, **_kwargs: object())
    try:
        worker._pipeline_pending_summary(task)
    finally:
        worker.close()

    with Session(engine) as session:
        row = session.get(PipelineTask, task_id)
        assert row.status == "completed"
        assert row.stage == "pending_summary"
        assert row.error_code is None


def test_notify_feishu_skips_when_disabled_even_with_env(config_file: Path, monkeypatch) -> None:
    # Plan §1.1: feishu.enabled=false must never send, even if env vars exist.
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    monkeypatch.setenv("FEISHU_OA_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/x")
    monkeypatch.setenv("FEISHU_OA_SECRET", "secret")
    queue = ProductionQueue(engine)
    task_id = queue.enqueue(
        "realtime_pending", "1", "notify_feishu", "notify-feishu-skip",
        payload={"occurrence_id": -1, "notify": True},
    )
    task = queue.claim("worker-test")
    worker = OperationWorker(settings, config_path=config_file)
    worker.owner = "worker-test"
    sent = []
    monkeypatch.setattr(
        "oa_knowledge.notifications.feishu_service.FeishuService.send_pending_summary",
        lambda self, *args, **kwargs: sent.append(DeliveryResult("sent", False)) or DeliveryResult("sent", False),
    )
    try:
        worker._pipeline_notify_feishu(task)
    finally:
        worker.close()

    # Disabled -> no delivery attempted, task completes.
    assert sent == []
    with Session(engine) as session:
        row = session.get(PipelineTask, task_id)
        assert row.status == "completed"


def test_notify_feishu_delivers_and_records_delivery(config_file: Path, monkeypatch) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    from oa_knowledge.db.models import (
        ItemOccurrence,
        ItemSnapshot,
        LogicalItem,
        NotificationDelivery,
        SummaryJob,
        SummaryVersion,
    )

    monkeypatch.setenv("FEISHU_OA_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/x")
    monkeypatch.setenv("FEISHU_OA_SECRET", "secret")
    settings.feishu.enabled = True
    with Session(engine) as session:
        logical = LogicalItem(logical_key="pending:1", title="待办事项")
        session.add(logical); session.flush()
        occurrence = ItemOccurrence(
            logical_item_id=logical.id, channel="pending", occurrence_key="pending:1",
            affair_id_text="123", title="待办事项", sender="张三", current_node="审批中", deadline_text="2026-08-10",
        )
        session.add(occurrence); session.flush()
        snapshot = ItemSnapshot(logical_item_id=logical.id, snapshot_kind="pending_initial", version=1,
                                content_hash="0" * 64, payload_json="{}")
        session.add(snapshot); session.flush()
        job = SummaryJob(logical_item_id=logical.id, snapshot_id=snapshot.id, summary_kind="pending",
                         stage="item_summary", status="completed", idempotency_key="idem-1", max_attempts=3)
        session.add(job); session.flush()
        version = SummaryVersion(logical_item_id=logical.id, summary_job_id=job.id, snapshot_id=snapshot.id, summary_kind="pending",
                                 version=1, status="current", input_hash="abc123",
                                 structured_json='{"summary":"请审批","matter_type":"报销","current_stage":"审批中","required_action":"确认","risks":[],"deadlines":[],"key_points":[],"amounts":[],"attachment_overview":[],"confidence":0.9}',
                                 provider_name="test", model_name="test", prompt_version="pending-v1")
        session.add(version); session.commit()
        logical_id = logical.id
        occurrence_id = occurrence.id

    queue = ProductionQueue(engine)
    task_id = queue.enqueue(
        "realtime_pending", str(logical_id), "notify_feishu", "notify-feishu-deliver",
        payload={"occurrence_id": occurrence_id, "notify": True},
    )
    task = queue.claim("worker-test")
    worker = OperationWorker(settings, config_path=config_file)
    worker.owner = "worker-test"
    sent = []
    monkeypatch.setattr(
        "oa_knowledge.notifications.feishu_service.FeishuService.send_pending_summary",
        lambda self, *args, **kwargs: sent.append(kwargs) or DeliveryResult("sent", False),
    )
    try:
        worker._pipeline_notify_feishu(task)
    finally:
        worker.close()

    assert len(sent) == 1
    with Session(engine) as session:
        row = session.get(PipelineTask, task_id)
        assert row.status == "queued"
        assert row.stage == "pending_cleanup"
        delivery = session.scalar(select(NotificationDelivery).where(
            NotificationDelivery.idempotency_key == f"feishu:pending:{logical_id}:abc123"))
        assert delivery is not None
        assert delivery.status == "sent"
        assert delivery.sent_at is not None


def test_sent_pending_delivery_advances_to_cleanup_without_resend(config_file: Path, monkeypatch) -> None:
    settings = load_settings(config_file)
    settings.feishu.enabled = True
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    logical_id, occurrence_id = _seed_pending_summary(engine, monkeypatch)
    with Session(engine) as session:
        session.add(NotificationDelivery(
            logical_item_id=logical_id,
            channel="feishu",
            notification_type="pending_summary",
            idempotency_key=f"feishu:pending:{logical_id}:abc123",
            status="sent",
        ))
        session.commit()
    queue = ProductionQueue(engine)
    task_id = queue.enqueue(
        "realtime_pending", str(logical_id), "notify_feishu", "sent-then-cleanup",
        payload={"occurrence_id": occurrence_id, "notify": True},
    )
    task = queue.claim("worker-test")
    worker = OperationWorker(settings, config_path=config_file)
    worker.owner = "worker-test"
    monkeypatch.setattr(
        "oa_knowledge.notifications.feishu_service.FeishuService.send_pending_summary",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not resend")),
    )
    try:
        worker._pipeline_notify_feishu(task)
    finally:
        worker.close()

    with Session(engine) as session:
        row = session.get(PipelineTask, task_id)
        assert row.status == "queued"
        assert row.stage == "pending_cleanup"


def test_pending_cleanup_failure_is_requeued_without_resending(config_file: Path, monkeypatch) -> None:
    """Cleanup retries retain the sent delivery and never re-enter Feishu."""
    settings = load_settings(config_file)
    settings.feishu.enabled = True
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    logical_id, occurrence_id = _seed_pending_summary(engine, monkeypatch)
    with Session(engine) as session:
        session.add(NotificationDelivery(
            logical_item_id=logical_id,
            channel="feishu",
            notification_type="pending_summary",
            idempotency_key=f"feishu:pending:{logical_id}:abc123",
            status="sent",
        ))
        session.commit()

    queue = ProductionQueue(engine)
    task_id = queue.enqueue(
        "realtime_pending", str(logical_id), "pending_cleanup", "cleanup-retry-only",
        payload={"occurrence_id": occurrence_id, "notify": True},
    )
    task = queue.claim("worker-test")
    worker = OperationWorker(settings, config_path=config_file)
    worker.owner = "worker-test"

    def fail_cleanup(session, occurrence, *_args, **_kwargs):
        occurrence.cleanup_status = "cleanup_failed"
        occurrence.cleanup_error_code = "OSError"
        session.flush()
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr("oa_knowledge.pending_cleanup.perform_cleanup", fail_cleanup)
    try:
        worker._pipeline_pending_cleanup(task)
    finally:
        worker.close()

    with Session(engine) as session:
        row = session.get(PipelineTask, task_id)
        occurrence = session.get(ItemOccurrence, occurrence_id)
        assert row.status == "queued"
        assert row.stage == "pending_cleanup"
        assert row.error_code == "PENDING_CLEANUP_FAILED"
        assert occurrence.cleanup_status == "cleanup_failed"


def test_pipeline_done_capture_and_archive_archives_and_enqueues(config_file: Path, monkeypatch) -> None:
    # Done capture records only local evidence. Verification creates a separate
    # Markdown Delivery task; the archive task never enters parsing itself.
    from types import SimpleNamespace

    from oa_knowledge.collector import LoginState
    from oa_knowledge.constants import FileRole
    from oa_knowledge.db.models import ArchivedFile, MarkdownTask, OAItem, OAManifestItem
    from oa_knowledge.production_pipeline import ProductionQueue

    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        session.add(OAManifestItem(
            oa_item_key="done:abc", workitem_id_text="abc", title="已办事项",
            list_page=0, processing_status="pending_download",
        ))
        session.commit()
    queue = ProductionQueue(engine)
    task_id = queue.enqueue(
        "realtime_done", "done:abc", "done_capture_and_archive",
        "realtime-done:done:abc:na:archive-v2",
        payload={},
    )

    # Fake a DownloadedAttachment whose content passes integrity (stub inspect_file).
    attachment = SimpleNamespace(
        file_role=FileRole.DIRECT_ATTACHMENT, attachment_key="att-1", filename="doc.pdf",
        mime_type="application/pdf", size_bytes=10, content=b"%PDF-1.4 fake",
    )
    fake_capture = SimpleNamespace(
        page_family="done", detail_url="http://oa/done/abc", capture_issues=[],
        body=[SimpleNamespace(name="body.html", html="<html>body</html>")],
        workflow=[SimpleNamespace(name="wf.html", html="<html>wf</html>")],
        attachments=[attachment], related_containers=[],
    )

    class _FakeAdapter:
        def __init__(self, *args, **kwargs):
            pass
        def capture_direct(self, *args, **kwargs):
            return fake_capture

    class _FakeBrowser:
        page = MagicMock()
        base_url = "http://oa"
        def login_with_saved_credentials(self, *a, **k):
            return LoginState.AUTHENTICATED

    class _FakeBrowserCtx:
        def __enter__(self):
            return _FakeBrowser()
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        "oa_knowledge.detail_archive.inspect_file",
        lambda path, *_args, **_kwargs: SimpleNamespace(
            status="verified", valid=True, size_bytes=path.stat().st_size,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        ),
    )
    monkeypatch.setattr("oa_knowledge.collector.detail.CollaborationDetailAdapter", _FakeAdapter)
    monkeypatch.setattr("oa_knowledge.cli.verified_attachment_resolver", lambda *a, **k: object())
    monkeypatch.setattr("oa_knowledge.collector.browser.BrowserSession", lambda *a, **k: _FakeBrowserCtx())
    monkeypatch.setattr("oa_knowledge.resources.ResourceCoordinator",
                        lambda *a, **k: SimpleNamespace(acquire=lambda *a, **k: 1, release=lambda *a, **k: None))

    task = queue.claim("worker-test")
    worker = OperationWorker(settings, config_path=config_file)
    worker.owner = "worker-test"
    worker._pipeline_done_capture_and_archive(task)
    worker.close()

    with Session(engine) as session:
        item = session.scalar(select(OAItem).where(OAItem.oa_item_key == "done:abc"))
        assert item is not None
        assert item.source_channel == "done"
        assert session.scalar(select(ArchivedFile).where(
            ArchivedFile.oa_item_id == item.id, ArchivedFile.download_status == "verified",
            ArchivedFile.file_role == str(FileRole.DIRECT_ATTACHMENT),
        )) is not None
        assert session.scalar(select(MarkdownTask).where(MarkdownTask.source_file_id.is_not(None))) is None
        row = session.get(PipelineTask, task_id)
        assert row.status == "queued"
        assert row.stage == "archive_verify"

    verify_task = queue.claim("worker-test")
    worker = OperationWorker(settings, config_path=config_file)
    worker.owner = "worker-test"
    try:
        worker._pipeline_archive_verify(verify_task)
    finally:
        worker.close()

    with Session(engine) as session:
        assert session.get(PipelineTask, task_id).status == "completed"
        markdown_task = session.scalar(select(PipelineTask).where(
            PipelineTask.queue_name == "markdown_delivery",
            PipelineTask.logical_item_key == "done:abc",
        ))
        assert markdown_task is not None
        assert markdown_task.stage == "attachment_inventory"
        assert markdown_task.idempotency_key.startswith("markdown:done:abc:")


def test_pipeline_done_capture_and_archive_fails_when_manifest_missing(config_file: Path, monkeypatch) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    queue = ProductionQueue(engine)
    task_id = queue.enqueue("realtime_done", "done:ghost", "done_capture_and_archive", "realtime-done:done:ghost:na:archive-v2")
    task = queue.claim("worker-test")
    worker = OperationWorker(settings, config_path=config_file)
    worker.owner = "worker-test"
    worker._pipeline_done_capture_and_archive(task)
    worker.close()
    with Session(engine) as session:
        row = session.get(PipelineTask, task_id)
        assert row.status == "failed"
        assert row.error_code == "MANIFEST_MISSING"
    # Plan §1.1: feishu.enabled=true but webhook missing must fail loudly,
    # never treated as a successful send.
    settings = load_settings(config_file)
    settings.feishu.enabled = True
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    monkeypatch.delenv("FEISHU_OA_WEBHOOK", raising=False)
    monkeypatch.delenv("FEISHU_OA_SECRET", raising=False)
    queue = ProductionQueue(engine)
    task_id = queue.enqueue(
        "realtime_pending", "1", "notify_feishu", "notify-feishu-missing",
        payload={"occurrence_id": -1, "notify": True},
    )
    task = queue.claim("worker-test")
    worker = OperationWorker(settings, config_path=config_file)
    worker.owner = "worker-test"
    sent = []
    monkeypatch.setattr(
        "oa_knowledge.notifications.feishu_service.FeishuService.send_pending_summary",
        lambda self, *args, **kwargs: sent.append(DeliveryResult("sent", False)) or DeliveryResult("sent", False),
    )
    try:
        worker._pipeline_notify_feishu(task)
    finally:
        worker.close()

    assert sent == []
    with Session(engine) as session:
        row = session.get(PipelineTask, task_id)
        assert row.status == "failed"
        assert row.error_code == "FEISHU_MISCONFIGURED"


def _seed_pending_summary(engine, monkeypatch):
    """Create a logical item + current pending summary and return (engine, ids, queue, task)."""
    from oa_knowledge.db.models import (
        ItemOccurrence,
        ItemSnapshot,
        LogicalItem,
        SummaryJob,
        SummaryVersion,
    )

    monkeypatch.setenv("FEISHU_OA_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/x")
    monkeypatch.setenv("FEISHU_OA_SECRET", "secret")
    with Session(engine) as session:
        logical = LogicalItem(logical_key="pending:1", title="待办事项")
        session.add(logical); session.flush()
        occurrence = ItemOccurrence(
            logical_item_id=logical.id, channel="pending", occurrence_key="pending:1",
            affair_id_text="123", title="待办事项", sender="张三", current_node="审批中", deadline_text="2026-08-10",
        )
        session.add(occurrence); session.flush()
        snapshot = ItemSnapshot(logical_item_id=logical.id, snapshot_kind="pending_initial", version=1,
                                content_hash="0" * 64, payload_json="{}")
        session.add(snapshot); session.flush()
        job = SummaryJob(logical_item_id=logical.id, snapshot_id=snapshot.id, summary_kind="pending",
                         stage="item_summary", status="completed", idempotency_key="idem-1", max_attempts=3)
        session.add(job); session.flush()
        version = SummaryVersion(logical_item_id=logical.id, summary_job_id=job.id, snapshot_id=snapshot.id, summary_kind="pending",
                                 version=1, status="current", input_hash="abc123",
                                 structured_json='{"summary":"请审批","matter_type":"报销","current_stage":"审批中","required_action":"确认","risks":[],"deadlines":[],"key_points":[],"amounts":[],"attachment_overview":[],"confidence":0.9}',
                                 provider_name="test", model_name="test", prompt_version="pending-v1")
        session.add(version); session.commit()
        return logical.id, occurrence.id


def test_notify_feishu_retryable_error_requeues_task(config_file: Path, monkeypatch) -> None:
    # Plan §3.2: a retryable transport error must re-queue the task (not fail
    # permanently) and record the delivery as retry_wait.
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    settings.feishu.enabled = True
    logical_id, occurrence_id = _seed_pending_summary(engine, monkeypatch)

    queue = ProductionQueue(engine)
    task_id = queue.enqueue(
        "realtime_pending", str(logical_id), "notify_feishu", "notify-feishu-retry",
        payload={"occurrence_id": occurrence_id, "notify": True},
    )
    task = queue.claim("worker-test")
    worker = OperationWorker(settings, config_path=config_file)
    worker.owner = "worker-test"
    monkeypatch.setattr(
        "oa_knowledge.notifications.feishu_service.FeishuService.send_pending_summary",
        lambda self, *args, **kwargs: DeliveryResult("connect_failed", True, error_code="http_connect"),
    )
    try:
        worker._pipeline_notify_feishu(task)
    finally:
        worker.close()

    with Session(engine) as session:
        row = session.get(PipelineTask, task_id)
        assert row.status == "queued"
        assert row.recoverable is True
        delivery = session.scalar(select(NotificationDelivery).where(
            NotificationDelivery.idempotency_key == f"feishu:pending:{logical_id}:abc123"))
        assert delivery.status == "retry_wait"
        assert delivery.next_retry_at is not None


def test_notify_feishu_unknown_outcome_parks_for_manual_retry(config_file: Path, monkeypatch) -> None:
    # Plan §3.2: a read timeout (unknown_outcome) must NOT auto re-push; the
    # task fails non-recoverable and the delivery is parked as unknown.
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    settings.feishu.enabled = True
    logical_id, occurrence_id = _seed_pending_summary(engine, monkeypatch)

    queue = ProductionQueue(engine)
    task_id = queue.enqueue(
        "realtime_pending", str(logical_id), "notify_feishu", "notify-feishu-unknown",
        payload={"occurrence_id": occurrence_id, "notify": True},
    )
    task = queue.claim("worker-test")
    worker = OperationWorker(settings, config_path=config_file)
    worker.owner = "worker-test"
    monkeypatch.setattr(
        "oa_knowledge.notifications.feishu_service.FeishuService.send_pending_summary",
        lambda self, *args, **kwargs: DeliveryResult("unknown_outcome", False, safe_error="feishu request timed out; delivery outcome unknown"),
    )
    try:
        worker._pipeline_notify_feishu(task)
    finally:
        worker.close()

    with Session(engine) as session:
        row = session.get(PipelineTask, task_id)
        assert row.status == "failed"
        assert row.recoverable is False
        delivery = session.scalar(select(NotificationDelivery).where(
            NotificationDelivery.idempotency_key == f"feishu:pending:{logical_id}:abc123"))
        assert delivery.status == "unknown"
        assert delivery.next_retry_at is None
