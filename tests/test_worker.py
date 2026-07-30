from pathlib import Path
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, BatchItem, CollectionBatch, OAItem, OperationJob, ParseJob, PipelineTask, ReviewEntry
from oa_knowledge.web.worker import OperationWorker
from oa_knowledge.production_pipeline import ProductionQueue


def test_retry_progress_keeps_original_total_after_resume() -> None:
    assert OperationWorker._retry_progress(total_targets=2393, resumed=1118, completed_after_resume=1) == 1119


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
    ProductionQueue(engine).enqueue("historical_done_backfill", "done-1", "attachment_inventory", "production-1")
    worker = OperationWorker(settings, config_path=config_file)
    handled = []
    monkeypatch.setattr(worker, "_execute_pipeline_task", lambda task: handled.append(task.id))
    try:
        assert worker.run_once() is True
    finally:
        worker.close()
    assert len(handled) == 1


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
    ]

    selected = OperationWorker._historical_source_files(files)

    assert [file.id for file in selected] == [5, 6]


def test_history_without_attachment_evidence_completes_without_llm_failure(config_file: Path, monkeypatch) -> None:
    from oa_knowledge.done_knowledge import NoAttachmentEvidence

    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    queue = ProductionQueue(engine)
    task_id = queue.enqueue("historical_done_backfill", "done:no-attachments", "ollama_extract", "no-attachments")
    task = queue.claim("worker-test")
    worker = OperationWorker(settings, config_path=config_file)
    worker.owner = "worker-test"
    monkeypatch.setattr(
        "oa_knowledge.done_knowledge.generate_done_knowledge",
        lambda *args, **kwargs: (_ for _ in ()).throw(NoAttachmentEvidence()),
    )
    try:
        worker._pipeline_done_knowledge(task)
    finally:
        worker.close()

    with Session(engine) as session:
        row = session.get(PipelineTask, task_id)
        assert row.status == "completed"
        assert row.error_code is None


def test_pending_summary_with_notification_requested_never_enters_unsupported_stage(config_file: Path, monkeypatch) -> None:
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
        assert row.status == "completed"
        assert row.stage == "pending_summary"
        assert row.error_code is None
