from pathlib import Path
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, BatchItem, CollectionBatch, NotificationDelivery, OAItem, OperationJob, ParseJob, PipelineTask, ReviewEntry
from oa_knowledge.web.worker import OperationWorker
from oa_knowledge.production_pipeline import ProductionQueue
from oa_knowledge.notifications.models import DeliveryResult


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
        assert row.status == "completed"
        delivery = session.scalar(select(NotificationDelivery).where(
            NotificationDelivery.idempotency_key == f"feishu:pending:{logical_id}:abc123"))
        assert delivery is not None
        assert delivery.status == "sent"
        assert delivery.sent_at is not None


def test_notify_feishu_missing_webhook_fails_without_silent_success(config_file: Path, monkeypatch) -> None:
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
