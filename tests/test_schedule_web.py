from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.collector import LoginState
from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import (
    ArchivedFile,
    MarkdownTask,
    NotificationDelivery,
    OAItem,
    OperationJob,
    Run,
)
from oa_knowledge.web import create_web_app
from oa_knowledge.web.schedule_views import notifications_status, schedule_status


def _client(config_file: Path) -> TestClient:
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    upgrade_database(settings.database_path)
    return TestClient(create_web_app(settings))


def _csrf(client: TestClient) -> str:
    return client.get("/").cookies["oa_csrf"]


def _engine(config_file: Path):
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    upgrade_database(settings.database_path)
    return create_db_engine(settings.database_path)


def test_schedule_status_empty_on_new_database(config_file: Path) -> None:
    client = _client(config_file)
    payload = client.get("/api/schedule/status").json()
    assert payload["recent_runs"] == []
    assert payload["last_scan_at"] is None
    assert payload["summary"]["pending_new"] == 0
    assert payload["summary"]["done_new"] == 0
    assert payload["summary"]["markdown_backlog"] == 0
    assert "feishu" in payload["summary"]
    assert "oa_login" in payload["summary"]
    assert "nightly" in payload["summary"]


def test_schedule_status_reports_runs_and_summary(config_file: Path) -> None:
    engine = _engine(config_file)
    now = datetime.now(UTC)
    with Session(engine) as session:
        session.add(Run(
            run_key="scheduled_hourly:abc",
            stage="scheduled_hourly",
            status="completed",
            summary_json='{"pending": {"created": 3, "updated": 2, "unchanged": 5}, "done": {"new_items": 4}}',
            started_at=now - timedelta(minutes=30),
            finished_at=now - timedelta(minutes=29),
        ))
        session.add(Run(
            run_key="scheduled_nightly:def",
            stage="scheduled_nightly",
            status="completed",
            summary_json='{"done": {"markdown_tasks_enqueued": 7, "download_jobs_enqueued": 6}}',
            started_at=now - timedelta(hours=12),
            finished_at=now - timedelta(hours=12, minutes=5),
        ))
        session.add(NotificationDelivery(
            channel="feishu", notification_type="pending_summary",
            idempotency_key="id-1", status="sent", sent_at=now - timedelta(minutes=10),
        ))
        session.add(NotificationDelivery(
            channel="feishu", notification_type="pending_summary",
            idempotency_key="id-2", status="failed", error_code="connect_failed",
            updated_at=now - timedelta(minutes=5),
        ))
        item = OAItem(oa_item_key="done:backlog", source_channel="done", title="积压事项")
        session.add(item)
        session.flush()
        file_row = ArchivedFile(
            oa_item_id=item.id, attachment_key="main", source_container_key="root", depth=1,
            file_role="attachment", original_name="doc.pdf", local_relpath="sources/oa/done/doc.pdf",
        )
        session.add(file_row)
        session.flush()
        session.add(MarkdownTask(source_file_id=file_row.id, schema_version="v1", status="queued"))
        session.commit()

    client = _client(config_file)
    payload = client.get("/api/schedule/status").json()
    assert payload["last_scan_at"] is not None
    assert payload["summary"]["pending_new"] == 3
    assert payload["summary"]["pending_changed"] == 2
    assert payload["summary"]["done_new"] == 4
    assert payload["summary"]["markdown_backlog"] == 1
    assert payload["summary"]["feishu"]["sent"] == 1
    assert payload["summary"]["feishu"]["failed"] == 1
    assert payload["summary"]["oa_login"]["status"] == "authenticated"
    assert payload["summary"]["nightly"]["markdown_tasks_enqueued"] == 7
    assert payload["summary"]["nightly"]["download_jobs_enqueued"] == 6
    assert payload["notifications"]["last_success_at"] is not None
    assert payload["notifications"]["last_error_code"] == "connect_failed"


def test_schedule_status_is_reachable_via_shared_function(config_file: Path) -> None:
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True)
    upgrade_database(settings.database_path)
    payload = schedule_status(settings, limit=5)
    assert payload["recent_runs"] == []
    assert payload["summary"]["markdown_backlog"] == 0


def test_schedule_hourly_triggers_scan(config_file: Path) -> None:
    client = _client(config_file)
    response = client.post("/api/schedule/hourly", headers={"x-csrf-token": _csrf(client)})
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["stage"] == "hourly"
    assert isinstance(body["job_id"], int)
    engine = _engine(config_file)
    with Session(engine) as session:
        job = session.get(OperationJob, body["job_id"])
        assert job is not None
        assert job.job_type == "scheduled_hourly"
        assert job.status == "queued"


def test_schedule_nightly_triggers_scan(config_file: Path) -> None:
    client = _client(config_file)
    response = client.post("/api/schedule/nightly", headers={"x-csrf-token": _csrf(client)})
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["stage"] == "nightly"
    engine = _engine(config_file)
    with Session(engine) as session:
        job = session.get(OperationJob, body["job_id"])
        assert job is not None
        assert job.job_type == "scheduled_nightly"
        assert job.status == "queued"


def test_worker_runs_scheduled_hourly_job(config_file: Path) -> None:
    """Plan-0806-1 §9 (test two): POST enqueues a real job the worker executes into a real Run."""
    from oa_knowledge.web.worker import OperationWorker

    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        job = OperationJob(job_key="test1", job_type="scheduled_hourly", idempotency_key="scheduled:hourly:test1")
        session.add(job)
        session.commit()
        job_id = job.id
    engine.dispose()

    class _PendingDiscovery:
        items = []
        pages_scanned = 1
        source_total_pages = 1
        source_total_count = 0

    class _DonePage:
        items = []
        is_last_page = True
        source_total_pages = 1

    with patch("oa_knowledge.scheduled_sync.ResourceCoordinator") as RC, \
         patch("oa_knowledge.scheduled_sync.BrowserSession") as BS, \
         patch("oa_knowledge.scheduled_sync.DoneAdapter") as DA, \
         patch("oa_knowledge.scheduled_sync.PendingAdapter") as PA:
        coordinator = RC.return_value
        coordinator.acquire.return_value = 1
        browser = BS.return_value.__enter__.return_value
        browser.login_with_saved_credentials.return_value = LoginState.AUTHENTICATED
        browser.page = MagicMock()
        browser.base_url = "http://oa"
        DA.return_value.discover_page.return_value = _DonePage()
        PA.return_value.discover_all_pages.return_value = _PendingDiscovery()

        worker = OperationWorker(settings, config_path=config_file)
        try:
            assert worker.run_once() is True
            with Session(create_db_engine(settings.database_path)) as s2:
                finished = s2.get(OperationJob, job_id)
                assert finished.status == "completed"
                run = s2.scalar(select(Run).where(Run.stage == "scheduled_hourly"))
                assert run is not None
                assert run.status == "completed"
        finally:
            worker.close()


def test_notifications_status_reports_counts(config_file: Path) -> None:
    engine = _engine(config_file)
    now = datetime.now(UTC)
    with Session(engine) as session:
        session.add(NotificationDelivery(
            channel="feishu", notification_type="pending_summary",
            idempotency_key="n-1", status="sent", sent_at=now - timedelta(minutes=3),
        ))
        session.add(NotificationDelivery(
            channel="feishu", notification_type="pending_summary",
            idempotency_key="n-2", status="retry_wait", error_code="rate_limited",
            updated_at=now - timedelta(minutes=1),
        ))
        session.commit()

    client = _client(config_file)
    payload = client.get("/api/notifications/status").json()
    assert payload["counts"]["sent"] == 1
    assert payload["counts"]["retry_wait"] == 1
    assert payload["last_success_at"] is not None
    assert payload["last_error_code"] == "rate_limited"


def test_notifications_status_reachable_via_shared_function(config_file: Path) -> None:
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True)
    upgrade_database(settings.database_path)
    payload = notifications_status(settings)
    assert payload["counts"] == {}
    assert payload["feishu_state"] in {"ready", "disabled", "misconfigured"}


def test_notifications_test_sends_synthetic(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from oa_knowledge.notifications.feishu_service import DeliveryResult

    monkeypatch.setattr(
        "oa_knowledge.notifications.feishu_service.FeishuService.send_test",
        lambda self: DeliveryResult("sent", False, error_code=None),
    )
    monkeypatch.setattr(
        "oa_knowledge.web.schedule_views.validate_feishu_runtime_config",
        lambda settings: "ready",
    )
    client = _client(config_file)
    response = client.post("/api/notifications/test", headers={"x-csrf-token": _csrf(client)})
    assert response.status_code == 202
    assert response.json()["status"] == "sent"


def test_notifications_test_not_ready_returns_409(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "oa_knowledge.web.schedule_views.validate_feishu_runtime_config",
        lambda settings: "disabled",
    )
    client = _client(config_file)
    response = client.post("/api/notifications/test", headers={"x-csrf-token": _csrf(client)})
    assert response.status_code == 409
    assert response.json()["detail"] == "disabled"


def test_notifications_retry_reports_status(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from oa_knowledge.notifications.feishu_service import DeliveryResult

    def fake_retry(engine, settings, delivery_id):
        return DeliveryResult("sent", False, error_code=None)

    monkeypatch.setattr(
        "oa_knowledge.notifications.feishu_service.retry_pending_summary_delivery",
        fake_retry,
    )
    client = _client(config_file)
    response = client.post("/api/notifications/42/retry", headers={"x-csrf-token": _csrf(client)})
    assert response.status_code == 202
    assert response.json()["status"] == "sent"


def test_notifications_retry_failure_returns_409(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from oa_knowledge.notifications.feishu_service import DeliveryResult

    def fake_retry(engine, settings, delivery_id):
        return DeliveryResult("rejected", False, error_code="delivery_not_found")

    monkeypatch.setattr(
        "oa_knowledge.notifications.feishu_service.retry_pending_summary_delivery",
        fake_retry,
    )
    client = _client(config_file)
    response = client.post("/api/notifications/99/retry", headers={"x-csrf-token": _csrf(client)})
    assert response.status_code == 409
    assert response.json()["detail"] == "delivery_not_found"


def test_schedule_endpoints_require_csrf(config_file: Path) -> None:
    client = _client(config_file)
    assert client.post("/api/schedule/hourly").status_code == 403
    assert client.post("/api/notifications/test").status_code == 403
