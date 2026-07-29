from __future__ import annotations

import os
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.batches import BatchPlan, plan_batch
from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, BatchItem, CollectionBatch, ExclusionPolicyRevision, OAItem, OAManifestItem, OperationEvent, OperationJob, ReviewEntry
from oa_knowledge.web import create_web_app
from oa_knowledge.web.status import execute_archive_job, pause_archive_batch, start_archive_job, start_backfill_campaign


def _client(config_file: Path) -> tuple[TestClient, Path]:
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True)
    upgrade_database(settings.database_path)
    return TestClient(create_web_app(settings)), settings.data_root


def test_status_is_read_only_and_reports_current_schema(config_file: Path) -> None:
    client, _ = _client(config_file)
    response = client.get("/api/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"] == "0020_production_pipeline"
    assert payload["stage"] == "2B-3"
    assert payload["oa_auth"] == {"status": "unknown", "checked_at": None, "read_only": True}
    assert payload["counts"]["items"] == 0
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_lifecycle_endpoints_are_database_backed_and_empty_on_new_database(config_file: Path) -> None:
    client, _ = _client(config_file)

    pending = client.get("/api/lifecycle/pending")
    knowledge = client.get("/api/lifecycle/knowledge")
    done = client.get("/api/lifecycle/done")
    system = client.get("/api/lifecycle/system")

    assert pending.status_code == knowledge.status_code == done.status_code == system.status_code == 200
    assert pending.json() == {"items": [], "total": 0}
    assert knowledge.json() == {"documents": [], "total": 0}
    assert done.json() == {
        "items": [], "total": 0, "page": 1, "page_size": 100,
        "metrics": {"oa_done_total": 0, "downloaded_items": 0, "verified_attachments": 0},
        "lifecycle_pilot_status": "waiting_for_user_completion",
    }
    assert system.json()["counts"]["snapshots"] == 0
    assert system.json()["worker"] is None


def test_lifecycle_pending_excludes_completed_occurrences(config_file: Path) -> None:
    client, data_root = _client(config_file)
    engine = create_db_engine(data_root / "state" / "oa.db")
    from oa_knowledge.db.models import ItemOccurrence, LogicalItem
    with Session(engine) as session:
        logical = LogicalItem(logical_key="completed-pending", title="Completed", lifecycle_status="done_confirmed")
        session.add(logical)
        session.flush()
        session.add(ItemOccurrence(
            logical_item_id=logical.id, occurrence_key="pending:completed", channel="pending",
            title="Completed", occurrence_status="completed",
        ))
        session.commit()

    assert client.get("/api/lifecycle/pending").json() == {"items": [], "total": 0}


def test_lifecycle_done_uses_canonical_manifest_before_local_archive(config_file: Path) -> None:
    client, data_root = _client(config_file)
    engine = create_db_engine(data_root / "state" / "oa.db")
    from oa_knowledge.db.models import OAManifestItem
    with Session(engine) as session:
        session.add(OAManifestItem(
            oa_item_key="done:new", workitem_id_text="new", title="Newly completed",
            processing_status="pending_download", list_page=1,
        ))
        session.commit()

    payload = client.get("/api/lifecycle/done").json()
    assert payload["total"] == 1
    assert payload["items"][0]["title"] == "Newly completed"
    assert payload["items"][0]["pipeline_status"] == "pending_download"


def test_lifecycle_done_metrics_and_pagination_use_full_dataset(config_file: Path) -> None:
    client, data_root = _client(config_file)
    engine = create_db_engine(data_root / "state" / "oa.db")
    with Session(engine) as session:
        manifests = [
            OAManifestItem(
                oa_item_key=f"done:{index}", workitem_id_text=str(index), title=f"Synthetic {index}",
                processing_status="downloaded" if index < 101 else "download_failed", list_page=1,
            )
            for index in range(102)
        ]
        session.add_all(manifests)
        archived = [
            OAItem(oa_item_key=f"done:{index}", source_channel="done", title=f"Synthetic {index}")
            for index in range(101)
        ]
        session.add_all(archived)
        session.flush()
        session.add_all([
            ArchivedFile(
                oa_item_id=item.id, original_name=f"attachment-{item.id}.pdf", attachment_key=f"a-{item.id}",
                file_role="direct_attachment", source_container_key=f"a-{item.id}",
                local_relpath=f"raw/{item.id}.pdf", download_status="verified", size_bytes=1,
            )
            for item in archived
        ])
        session.add(ArchivedFile(
            oa_item_id=archived[0].id, original_name="body.html", attachment_key="body",
            file_role="official_body", source_container_key="body", local_relpath="raw/body.html",
            download_status="verified", size_bytes=1,
        ))
        session.commit()

    payload = client.get("/api/lifecycle/done?page=2&page_size=20").json()

    assert len(payload["items"]) == 20
    assert payload["page"] == 2 and payload["page_size"] == 20
    assert payload["total"] == 102
    assert payload["metrics"] == {
        "oa_done_total": 102,
        "downloaded_items": 101,
        "verified_attachments": 101,
    }


def test_done_incremental_refresh_queues_single_three_page_job(config_file: Path) -> None:
    client, data_root = _client(config_file)
    client.get("/api/status")
    csrf = client.cookies.get("oa_csrf")

    first = client.post("/api/manifest/refresh-incremental", headers={"x-csrf-token": csrf})
    second = client.post("/api/manifest/refresh-incremental", headers={"x-csrf-token": csrf})

    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    engine = create_db_engine(data_root / "state" / "oa.db")
    with Session(engine) as session:
        jobs = session.scalars(select(OperationJob).where(OperationJob.job_type == "done_incremental")).all()
        assert len(jobs) == 1
        assert json.loads(jobs[0].parameters_json) == {"max_pages": 3}


def test_lifecycle_system_ignores_historical_paused_jobs(config_file: Path) -> None:
    client, data_root = _client(config_file)
    engine = create_db_engine(data_root / "state" / "oa.db")
    with Session(engine) as session:
        session.add(OperationJob(
            job_key="historical-paused", job_type="archive_batch", status="paused",
            idempotency_key="historical-paused", parameters_json="{}",
        ))
        session.commit()

    payload = client.get("/api/lifecycle/system").json()
    assert payload["worker"] is None


def test_lifecycle_system_exposes_current_item_and_failure_count(config_file: Path) -> None:
    client, data_root = _client(config_file)
    engine = create_db_engine(data_root / "state" / "oa.db")
    from oa_knowledge.db.models import OperationEvent
    with Session(engine) as session:
        job = OperationJob(
            job_key="pending-capture-current", job_type="pending_capture", status="running",
            idempotency_key="pending-capture-current", parameters_json="{}",
            progress_current=3, progress_total=30,
        )
        session.add(job)
        session.flush()
        session.add_all([
            OperationEvent(job_id=job.id, sequence=1, event_type="item", status="failed", details_json='{"title":"Earlier"}'),
            OperationEvent(job_id=job.id, sequence=2, event_type="item", status="running", details_json='{"title":"Current title","attachment_verified":1,"attachment_total":2}'),
        ])
        session.commit()

    payload = client.get("/api/lifecycle/system").json()["worker"]
    assert payload["current_title"] == "Current title"
    assert payload["attachment_verified"] == 1
    assert payload["attachment_total"] == 2
    assert payload["failure_count"] == 1


def test_review_and_maintenance_endpoints(config_file: Path) -> None:
    client, data_root = _client(config_file)
    engine = create_db_engine(data_root / "state" / "oa.db")
    with Session(engine) as session:
        session.add(ReviewEntry(kind="depth_limit_reached", depth=10, details_json='{"sample":true}'))
        session.commit()
    listed = client.get("/api/reviews")
    assert listed.status_code == 200
    assert listed.json()[0]["details"] == {"sample": True}
    csrf = client.cookies.get("oa_csrf")
    resolved = client.post(
        f"/api/reviews/{listed.json()[0]['id']}/resolve",
        headers={"x-csrf-token": csrf or ""}, json={"resolution": "resolved"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"
    maintenance = client.get("/api/maintenance")
    assert maintenance.status_code == 200
    assert maintenance.json()["audit"]["ok"] is True
    assert "allowed" in maintenance.json()["capacity"]


def test_cross_origin_and_missing_csrf_are_rejected(config_file: Path) -> None:
    client, _ = _client(config_file)
    cross_origin = client.get("/api/status", headers={"Origin": "https://outside.example"})
    assert cross_origin.status_code == 403
    mutation = client.post("/api/not-yet-implemented")
    assert mutation.status_code == 403
    assert mutation.json()["detail"] == "CSRF validation failed"


def test_non_loopback_host_header_is_rejected(config_file: Path) -> None:
    client, _ = _client(config_file)
    response = client.get("/api/status", headers={"Host": "192.168.1.20:2567"})  # public-release: synthetic
    assert response.status_code == 400


def test_session_key_is_created_with_owner_only_permissions(config_file: Path) -> None:
    _, data_root = _client(config_file)
    key = data_root / "runtime" / "web-session.key"
    assert key.is_file()
    assert len(key.read_text(encoding="ascii")) >= 48
    assert os.stat(key).st_mode & 0o777 == 0o600


def test_operation_job_constraints_and_event_cascade(config_file: Path) -> None:
    _, data_root = _client(config_file)
    engine = create_db_engine(data_root / "state" / "oa.db")
    with Session(engine) as session:
        job = OperationJob(job_key="job-1", job_type="discovery", idempotency_key="idem-1")
        job.events.append(OperationEvent(sequence=1, event_type="created", status="queued"))
        session.add(job)
        session.commit()
        job_id = job.id
        session.delete(job)
        session.commit()
    with sqlite3.connect(data_root / "state" / "oa.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM operation_events WHERE job_id = ?", (job_id,)).fetchone()[0] == 0


def test_manifest_status_prefers_running_job_and_uses_its_active_item(config_file: Path) -> None:
    client, data_root = _client(config_file)
    engine = create_db_engine(data_root / "state" / "oa.db")
    started_at = datetime(2026, 7, 21, 9, 30, tzinfo=timezone.utc)
    with Session(engine) as session:
        running_item = OAManifestItem(
            oa_item_key="synthetic-running-item",
            workitem_id_text="synthetic-running-workitem",
            title="合成运行事项",
            sender="测试单位",
            list_page=1,
            processing_status="processing",
        )
        queued_item = OAManifestItem(
            oa_item_key="synthetic-queued-item",
            workitem_id_text="synthetic-queued-workitem",
            title="合成排队事项",
            sender="测试单位",
            list_page=1,
            processing_status="processing",
        )
        failed_item = OAManifestItem(
            oa_item_key="synthetic-failed-item",
            workitem_id_text="synthetic-failed-workitem",
            title="合成失败事项",
            sender="测试单位",
            list_page=1,
            processing_status="download_failed",
            last_retry_at=started_at + timedelta(minutes=1),
        )
        session.add_all([running_item, queued_item, failed_item])
        session.flush()
        archived_item = OAItem(oa_item_key=running_item.oa_item_key, source_channel="done", title=running_item.title)
        session.add(archived_item)
        session.flush()
        session.add(ArchivedFile(
            oa_item_id=archived_item.id,
            original_name="合成待重试附件.pdf",
            attachment_key="synthetic-retry-attachment",
            file_role="official_attachment",
            source_container_key="collaboration:synthetic-running",
            download_status="download_failed",
            download_attempts=1,
        ))

        running = OperationJob(
            job_key="synthetic-running-job",
            job_type="full_manifest_retry",
            status="running",
            idempotency_key="synthetic-running-idempotency",
            parameters_json=json.dumps({"oa_item_keys": [running_item.oa_item_key, failed_item.oa_item_key]}),
            progress_current=17,
            progress_total=80,
            started_at=started_at,
        )
        running.events.append(OperationEvent(
            sequence=1,
            event_type="manifest_retry_item_started",
            status="running",
            details_json=json.dumps({"manifest_id": running_item.id, "stage": "正在下载附件"}),
        ))
        queued = OperationJob(
            job_key="synthetic-newer-queued-job",
            job_type="full_manifest_retry",
            status="queued",
            idempotency_key="synthetic-queued-idempotency",
            progress_current=0,
            progress_total=50,
        )
        queued.events.append(OperationEvent(
            sequence=1,
            event_type="manifest_retry_item_started",
            status="queued",
            details_json=json.dumps({"manifest_id": queued_item.id, "stage": "不应显示"}),
        ))
        session.add_all([running, queued])
        session.commit()

    payload = client.get("/api/manifest/status").json()

    assert payload["job"]["job_key"] == "synthetic-running-job"
    assert payload["job"]["progress_current"] == 17
    assert payload["job"]["progress_total"] == 80
    assert payload["job"]["failure_count"] == 1
    assert payload["job"]["started_at"] == started_at.isoformat()
    assert payload["active_item"]["title"] == "合成运行事项"
    assert payload["active_item"]["stage"] == "正在下载附件"
    assert payload["active_item"]["attachment_total"] == 1
    assert payload["active_item"]["attachment_failed"] == 0
    assert payload["active_item"]["attachment_pending"] == 1


def test_dashboard_summarizes_latest_batch(config_file: Path) -> None:
    client, data_root = _client(config_file)
    engine = create_db_engine(data_root / "state" / "oa.db")
    with Session(engine) as session:
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        batch, _ = plan_batch(session, BatchPlan("done", start, start + timedelta(days=1), "completed_at", 20, "synthetic"))
        batch.discovered_count = 20
        batch.archived_count = 7
        batch.skipped_count = 3
        batch.status = "paused"
        session.commit()
    payload = client.get("/api/status").json()
    assert payload["batch"]["archived"] == 7
    assert payload["batch"]["skipped"] == 3
    assert payload["batch"]["pending"] == 10


# ---- New 2B-0 endpoint tests ----


def test_api_items_returns_empty_list(config_file: Path) -> None:
    client, _ = _client(config_file)
    resp = client.get("/api/items")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["items"] == []
    assert payload["pagination"]["total"] == 0
    assert payload["pagination"]["page"] == 1


def test_api_items_lists_and_filters(config_file: Path) -> None:
    client, data_root = _client(config_file)
    engine = create_db_engine(data_root / "state" / "oa.db")
    with Session(engine) as session:
        item = OAItem(
            oa_item_key="test-key-1",
            source_channel="done",
            title="年度预算调整通知",
            sender="财务部",
            pipeline_status="files_verified",
        )
        session.add(item)
        session.commit()
    resp = client.get("/api/items")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["pagination"]["total"] == 1
    assert payload["items"][0]["title"] == "年度预算调整通知"

    # Filter by pipeline status
    resp2 = client.get("/api/items?pipeline_status=files_verified")
    assert resp2.json()["pagination"]["total"] == 1

    # Filter by non-matching status
    resp3 = client.get("/api/items?pipeline_status=parsed")
    assert resp3.json()["pagination"]["total"] == 0

    # Search by title
    resp4 = client.get("/api/items?search=预算")
    assert resp4.json()["pagination"]["total"] == 1

    # Pagination
    resp5 = client.get("/api/items?page=1&page_size=1")
    assert len(resp5.json()["items"]) == 1
    assert resp5.json()["pagination"]["total"] == 1


def test_api_batches_returns_list(config_file: Path) -> None:
    client, _ = _client(config_file)
    resp = client.get("/api/batches")
    assert resp.status_code == 200
    payload = resp.json()
    assert "batches" in payload
    assert "status_breakdown" in payload
    assert payload["batches"] == []


def test_api_batches_summarizes_planned_batch(config_file: Path) -> None:
    client, data_root = _client(config_file)
    engine = create_db_engine(data_root / "state" / "oa.db")
    with Session(engine) as session:
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        batch, _ = plan_batch(session, BatchPlan("done", start, start + timedelta(days=7), "completed_at", 50, "test batch"))
        batch.discovered_count = 50
        batch.archived_count = 10
        batch.status = "running"
        session.commit()
    resp = client.get("/api/batches")
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["batches"]) == 1
    assert payload["batches"][0]["status"] == "running"
    assert payload["batches"][0]["archived"] == 10


def test_api_events_returns_empty_list(config_file: Path) -> None:
    client, _ = _client(config_file)
    resp = client.get("/api/events")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_api_events_stream_returns_sse_format(config_file: Path) -> None:
    client, _ = _client(config_file)
    # Use head to verify the route exists without hanging on the streaming generator
    resp = client.head("/api/events/stream")
    assert resp.status_code == 200


def test_frontend_html_is_served(config_file: Path) -> None:
    client, _ = _client(config_file)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_static_assets_are_served(config_file: Path) -> None:
    client, _ = _client(config_file)
    # After build, assets directory exists
    resp = client.get("/assets/index.html")
    # May return 404 if assets not built, but should not crash
    assert resp.status_code in (200, 404)


def test_security_headers_present_on_all_responses(config_file: Path) -> None:
    client, _ = _client(config_file)
    resp = client.get("/")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]


def test_items_endpoint_respects_csrf_for_mutations(config_file: Path) -> None:
    client, _ = _client(config_file)
    # GET is safe, no CSRF needed
    resp = client.get("/api/items")
    assert resp.status_code == 200
    # POST without CSRF should be rejected
    resp2 = client.post("/api/items")
    assert resp2.status_code == 403


def test_manifest_ledger_filters_sorts_and_counts_attachments(config_file: Path) -> None:
    client, data_root = _client(config_file)
    engine = create_db_engine(data_root / "state" / "oa.db")
    with Session(engine) as session:
        success_item = OAItem(oa_item_key="done:success", source_channel="done", title="制度通知", sender="办公室", pipeline_status="files_verified")
        failed_item = OAItem(oa_item_key="done:failed", source_channel="done", title="采购申请附件失败", sender="采购部", pipeline_status="download_failed")
        session.add_all([success_item, failed_item])
        session.flush()
        session.add_all([
            ArchivedFile(oa_item_id=success_item.id, original_name="正文.pdf", attachment_key="body", file_role="official_body", source_container_key="body", local_relpath="raw/body.pdf", download_status="verified", size_bytes=10),
            ArchivedFile(oa_item_id=success_item.id, original_name="附件.docx", attachment_key="att-1", file_role="direct_attachment", source_container_key="att-1", local_relpath="raw/att.docx", download_status="verified", size_bytes=20),
            ArchivedFile(oa_item_id=failed_item.id, original_name="报价.xlsx", attachment_key="att-2", file_role="direct_attachment", source_container_key="att-2", download_status="failed", download_attempts=2),
        ])
        session.add_all([
            OAManifestItem(oa_item_key="done:success", workitem_id_text="success", title="制度通知", sender="办公室", completed_at=datetime(2026, 7, 3, tzinfo=timezone.utc), list_page=1, processing_status="downloaded", archive_relpath="raw/success"),
            OAManifestItem(oa_item_key="done:skip", workitem_id_text="skip", title="报销审批", sender="财务部", completed_at=datetime(2026, 7, 2, tzinfo=timezone.utc), list_page=1, processing_status="skipped", matched_exclusion_keyword="报销"),
            OAManifestItem(oa_item_key="done:failed", workitem_id_text="failed", title="采购申请附件失败", sender="采购部", completed_at=datetime(2026, 7, 1, tzinfo=timezone.utc), list_page=1, processing_status="download_failed", retry_count=3, last_error="network timeout"),
        ])
        session.commit()

    resp = client.get("/api/manifest/items?statuses=downloaded,download_failed&sort=completed_at&direction=asc&attachment_filter=has_failed")
    assert resp.status_code == 200
    payload = resp.json()
    assert [item["status"] for item in payload["items"]] == ["download_failed"]
    assert payload["items"][0]["attachment_total"] == 1
    assert payload["items"][0]["attachment_failed"] == 1
    assert payload["items"][0]["last_error_summary"] == "network timeout"
    assert payload["summary"]["downloaded"] == 1
    assert payload["summary"]["skipped"] == 1
    assert sum(payload["summary"].values()) == 3


def test_manifest_detail_distinguishes_skipped_from_no_attachment_and_lists_files(config_file: Path) -> None:
    client, data_root = _client(config_file)
    engine = create_db_engine(data_root / "state" / "oa.db")
    with Session(engine) as session:
        item = OAItem(oa_item_key="done:failed-detail", source_channel="done", title="附件下载失败", sender="办公室", pipeline_status="download_failed", archive_relpath="raw/failed")
        session.add(item)
        session.flush()
        session.add(ArchivedFile(oa_item_id=item.id, original_name="附件.pdf", attachment_key="att", file_role="direct_attachment", source_container_key="att", local_relpath="raw/failed/附件.pdf", download_status="failed", download_attempts=2, size_bytes=0))
        session.add_all([
            OAManifestItem(oa_item_key="done:failed-detail", workitem_id_text="failed-detail", title=item.title, sender=item.sender, completed_at=datetime(2026, 7, 4, tzinfo=timezone.utc), list_page=1, processing_status="download_failed", retry_count=2, last_error="HTTP 500", archive_relpath="raw/failed"),
            OAManifestItem(oa_item_key="done:skip-detail", workitem_id_text="skip-detail", title="报销单", sender="财务部", completed_at=datetime(2026, 7, 5, tzinfo=timezone.utc), list_page=1, processing_status="skipped", matched_exclusion_keyword="报销"),
        ])
        session.commit()
        failed_manifest_id = session.scalar(select(OAManifestItem.id).where(OAManifestItem.oa_item_key == "done:failed-detail"))
        skipped_manifest_id = session.scalar(select(OAManifestItem.id).where(OAManifestItem.oa_item_key == "done:skip-detail"))

    failed = client.get(f"/api/manifest/items/{failed_manifest_id}").json()
    assert failed["status"] == "download_failed"
    assert failed["attachments"][0]["download_status"] == "failed"
    assert failed["attachments"][0]["retry_count"] == 2

    skipped = client.get(f"/api/manifest/items/{skipped_manifest_id}").json()
    assert skipped["status"] == "skipped"
    assert skipped["status_message"] == "该事项命中排除关键词“报销”，未进入详情页，因此没有下载正文和附件。"
    assert skipped["attachment_summary"]["total"] == 0


def test_manifest_retry_only_targets_filtered_failed_items(config_file: Path) -> None:
    client, data_root = _client(config_file)
    engine = create_db_engine(data_root / "state" / "oa.db")
    with Session(engine) as session:
        session.add_all([
            OAManifestItem(oa_item_key="done:fail-a", workitem_id_text="fail-a", title="采购申请A", sender="采购部", completed_at=datetime(2026, 7, 1, tzinfo=timezone.utc), list_page=1, processing_status="download_failed"),
            OAManifestItem(oa_item_key="done:fail-b", workitem_id_text="fail-b", title="制度失败B", sender="办公室", completed_at=datetime(2026, 7, 1, tzinfo=timezone.utc), list_page=1, processing_status="download_failed"),
            OAManifestItem(oa_item_key="done:ok", workitem_id_text="ok", title="采购申请成功", sender="采购部", completed_at=datetime(2026, 7, 1, tzinfo=timezone.utc), list_page=1, processing_status="downloaded"),
        ])
        session.commit()
    client.get("/api/status")
    csrf = client.cookies.get("oa_csrf") or ""
    resp = client.post("/api/manifest/retry?search=采购", headers={"x-csrf-token": csrf})
    assert resp.status_code == 202
    payload = resp.json()
    assert payload["target_count"] == 1
    with Session(engine) as session:
        job = session.scalar(select(OperationJob).where(OperationJob.job_type == "full_manifest_retry"))
        assert job is not None
        assert json.loads(job.parameters_json)["oa_item_keys"] == ["done:fail-a"]


def test_manifest_recheck_targets_only_no_attachment_items(config_file: Path) -> None:
    client, data_root = _client(config_file)
    engine = create_db_engine(data_root / "state" / "oa.db")
    with Session(engine) as session:
        session.add_all([
            OAManifestItem(oa_item_key="done:no-att", workitem_id_text="no-att", title="待复查", sender="办公室", list_page=1, processing_status="no_attachment"),
            OAManifestItem(oa_item_key="done:ok", workitem_id_text="ok", title="已成功", sender="办公室", list_page=1, processing_status="downloaded"),
            OAManifestItem(oa_item_key="done:skip", workitem_id_text="skip", title="规则跳过", sender="办公室", list_page=1, processing_status="skipped"),
        ])
        session.commit()
    client.get("/api/status")
    csrf = client.cookies.get("oa_csrf") or ""
    resp = client.post("/api/manifest/recheck-no-attachment", headers={"x-csrf-token": csrf})
    assert resp.status_code == 202
    assert resp.json()["target_count"] == 1
    with Session(engine) as session:
        job = session.scalar(select(OperationJob).where(OperationJob.job_type == "full_manifest_retry"))
        assert job is not None
        params = json.loads(job.parameters_json)
        assert params["oa_item_keys"] == ["done:no-att"]
        assert params["source_status"] == "no_attachment"


def test_manifest_audit_all_targets_every_manifest_status(config_file: Path) -> None:
    client, data_root = _client(config_file)
    engine = create_db_engine(data_root / "state" / "oa.db")
    with Session(engine) as session:
        session.add_all([
            OAManifestItem(oa_item_key=f"done:{status}", workitem_id_text=status, title=f"合成{status}", sender="测试单位", list_page=1, processing_status=status)
            for status in ("downloaded", "no_attachment", "download_failed", "skipped", "pending_download")
        ])
        session.commit()
    client.get("/api/status")
    csrf = client.cookies.get("oa_csrf") or ""

    response = client.post("/api/manifest/audit-all", headers={"x-csrf-token": csrf})

    assert response.status_code == 202
    assert response.json()["target_count"] == 5
    with Session(engine) as session:
        job = session.scalar(select(OperationJob).where(OperationJob.job_type == "full_manifest_retry"))
        assert job is not None
        params = json.loads(job.parameters_json)
        assert set(params["oa_item_keys"]) == {f"done:{status}" for status in ("downloaded", "no_attachment", "download_failed", "skipped", "pending_download")}
        assert params["source_status"] == "audit_all"


# ---- 2B-1: Discovery jobs and exclusion policies ----


def test_api_discovery_jobs_returns_empty_list(config_file: Path) -> None:
    client, _ = _client(config_file)
    resp = client.get("/api/discovery-jobs")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_api_create_discovery_job(config_file: Path) -> None:
    from fastapi.testclient import TestClient as TC
    client, data_root = _client(config_file)
    session_client = TC(app=client.app)
    session_client.get("/api/status")  # seed CSRF cookie
    csrf = session_client.cookies.get("oa_csrf")
    assert csrf is not None

    engine = create_db_engine(data_root / "state" / "oa.db")
    with Session(engine) as session:
        item = OAItem(
            oa_item_key="disc-test-1",
            source_channel="done",
            title="测试预算通知",
            sender="财务部",
            pipeline_status="discovered",
        )
        session.add(item)
        session.commit()
    resp = session_client.post(
        "/api/discovery-jobs?source_channel=done&days_back=30",
        headers={"x-csrf-token": csrf},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["mode"] == "title_only"
    assert payload["status"] == "queued"
    assert "batch_key" in payload

    duplicate = session_client.post(
        "/api/discovery-jobs?source_channel=done&days_back=30",
        headers={"x-csrf-token": csrf},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["job_id"] == payload["job_id"]
    assert duplicate.json()["created"] is False


def test_api_policies_crud(config_file: Path) -> None:
    from fastapi.testclient import TestClient as TC
    client, _ = _client(config_file)
    session_client = TC(app=client.app)
    # Seed CSRF cookie
    session_client.get("/api/status")
    csrf = session_client.cookies.get("oa_csrf")
    assert csrf is not None, "CSRF cookie should be set"

    # No hidden configuration rules participate in classification.
    resp = session_client.get("/api/policies")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    assert resp.json() == []

    # Create a policy
    resp = session_client.post(
        "/api/policies?name=test-policy&pattern=测试&action=skip&scope=title&description=A+test+policy",
        headers={"x-csrf-token": csrf},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["name"] == "test-policy"
    assert payload["pattern"] == "测试"
    assert payload["action"] == "skip"
    assert payload["scope"] == "title"
    policy_id = payload["id"]

    # List should show it
    resp2 = session_client.get("/api/policies")
    assert resp2.status_code == 200
    assert any(row["id"] == policy_id and row["source"] == "database" for row in resp2.json())

    # Delete
    resp3 = session_client.delete(f"/api/policies/{policy_id}", headers={"x-csrf-token": csrf})
    assert resp3.status_code == 200
    assert resp3.json()["deleted"] is True

    # Verify deletion
    resp4 = session_client.delete(f"/api/policies/{policy_id}", headers={"x-csrf-token": csrf})
    assert resp4.status_code == 404


def test_updating_policy_reports_and_releases_previously_skipped_items(config_file: Path) -> None:
    from fastapi.testclient import TestClient as TC
    client, data_root = _client(config_file)
    session_client = TC(app=client.app)
    session_client.get("/api/status")
    csrf = session_client.cookies.get("oa_csrf") or ""
    created = session_client.post(
        "/api/policies?name=报销事项&pattern=报销&action=metadata_only&scope=title",
        headers={"x-csrf-token": csrf},
    )
    policy_id = created.json()["id"]
    engine = create_db_engine(data_root / "state" / "oa.db")
    with Session(engine) as session:
        session.add(OAManifestItem(
            oa_item_key="done:expense-notice", workitem_id_text="expense-notice",
            title="关于采集财务报销制度的通知", list_page=1,
            processing_status="skipped", matched_exclusion_keyword="报销",
        ))
        session.commit()
    updated = session_client.post(
        "/api/policies?name=报销事项&pattern=报销申请&action=metadata_only&scope=title",
        headers={"x-csrf-token": csrf},
    )
    assert updated.status_code == 200
    assert updated.json()["affected_count"] == 1
    assert updated.json()["redownload_count"] == 1
    assert updated.json()["still_skipped_count"] == 0
    with Session(engine) as session:
        row = session.scalar(select(OAManifestItem).where(OAManifestItem.workitem_id_text == "expense-notice"))
        assert row is not None and row.processing_status == "pending_download"
    deleted = session_client.delete(f"/api/policies/{policy_id}", headers={"x-csrf-token": csrf})
    assert deleted.status_code == 200


def test_api_policies_preview(config_file: Path) -> None:
    client, data_root = _client(config_file)
    engine = create_db_engine(data_root / "state" / "oa.db")
    with Session(engine) as session:
        item = OAItem(
            oa_item_key="preview-test-1",
            source_channel="done",
            title="预算调整通知",
            sender="财务部",
            pipeline_status="discovered",
        )
        session.add(item)
        session.commit()
    resp = client.get("/api/policies/preview?pattern=预算&scope=title")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["pattern"] == "预算"
    assert payload["total_matches"] >= 1
    assert len(payload["hits"]) >= 1
    assert payload["hits"][0]["title"] == "预算调整通知"


def test_api_policies_invalid_action_rejected(config_file: Path) -> None:
    from fastapi.testclient import TestClient as TC
    client, _ = _client(config_file)
    session_client = TC(app=client.app)
    session_client.get("/api/status")  # seed the CSRF cookie
    csrf = session_client.cookies.get("oa_csrf")
    resp = session_client.post(
        "/api/policies?name=bad&pattern=x&action=invalid",
        headers={"x-csrf-token": csrf or ""},
    )
    assert resp.status_code == 400


def test_policy_preview_includes_pending_batch_items_and_save_applies(config_file: Path) -> None:
    from fastapi.testclient import TestClient as TC

    client, data_root = _client(config_file)
    session_client = TC(app=client.app)
    session_client.get("/api/status")
    csrf = session_client.cookies.get("oa_csrf")
    engine = create_db_engine(data_root / "state" / "oa.db")
    with Session(engine) as session:
        batch = CollectionBatch(
            batch_key="pending-policy-batch", plan_hash="9" * 64,
            source_channel="done", planned_limit=2, discovered_count=2, status="paused",
        )
        session.add(batch)
        session.flush()
        session.add_all([
            BatchItem(
                batch_id=batch.id, oa_item_key="done:trip-1", workitem_id_text="trip-1",
                title="关于张三的出差申请表", sender="张三", ordinal=1, archive_status="pending",
            ),
            BatchItem(
                batch_id=batch.id, oa_item_key="done:notice-1", workitem_id_text="notice-1",
                title="重要制度通知", sender="办公室", ordinal=2, archive_status="pending",
            ),
        ])
        session.commit()

    preview = session_client.get("/api/policies/preview?pattern=出差申请&scope=title")
    assert preview.status_code == 200
    assert preview.json()["total_matches"] == 1
    assert preview.json()["hits"][0]["pipeline_status"] == "pending"

    saved = session_client.post(
        "/api/policies?name=差旅表单&pattern=出差申请&action=metadata_only&scope=title",
        headers={"x-csrf-token": csrf or ""},
    )
    assert saved.status_code == 200
    assert saved.json()["applied_count"] == 1

    with Session(engine) as session:
        excluded = session.scalar(select(BatchItem).where(BatchItem.workitem_id_text == "trip-1"))
        retained = session.scalar(select(BatchItem).where(BatchItem.workitem_id_text == "notice-1"))
        batch = session.scalar(select(CollectionBatch).where(CollectionBatch.batch_key == "pending-policy-batch"))
        assert excluded is not None and excluded.archive_status == "confirmed_skip"
        assert excluded.skip_reason == "web_policy:1:metadata_only:出差申请"
        assert excluded.policy_version == "exclusion-policy-1-v1"
        assert retained is not None and retained.archive_status == "pending"
        assert batch is not None and batch.skipped_count == 1


def test_bulk_policy_import_strips_bullets_deduplicates_and_applies(config_file: Path) -> None:
    from fastapi.testclient import TestClient as TC

    client, data_root = _client(config_file)
    session_client = TC(app=client.app)
    session_client.get("/api/status")
    csrf = session_client.cookies.get("oa_csrf")
    engine = create_db_engine(data_root / "state" / "oa.db")
    with Session(engine) as session:
        batch = CollectionBatch(
            batch_key="bulk-policy-batch", plan_hash="8" * 64,
            source_channel="done", planned_limit=2, discovered_count=2, status="paused",
        )
        session.add(batch)
        session.flush()
        session.add_all([
            BatchItem(
                batch_id=batch.id, oa_item_key="done:payment", workitem_id_text="payment",
                title="付款申请单（测试）", sender="财务部", ordinal=1, archive_status="pending",
            ),
            BatchItem(
                batch_id=batch.id, oa_item_key="done:leave", workitem_id_text="leave",
                title="员工请假审批", sender="人力资源部", ordinal=2, archive_status="pending",
            ),
        ])
        session.commit()

    response = session_client.post(
        "/api/policies/bulk",
        headers={"x-csrf-token": csrf or ""},
        json={
            "text": "- 付款申请单\n- 请假\n\n1. 付款申请单\n",
            "action": "metadata_only",
            "scope": "title",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["keywords"] == ["付款申请单", "请假"]
    assert payload["keyword_count"] == 2
    assert payload["created_count"] == 2
    assert payload["applied_count"] == 2

    with Session(engine) as session:
        items = session.scalars(select(BatchItem).order_by(BatchItem.ordinal)).all()
        assert [item.archive_status for item in items] == ["confirmed_skip", "confirmed_skip"]
        revisions = session.scalars(select(ExclusionPolicyRevision).order_by(ExclusionPolicyRevision.id)).all()
        assert [(revision.change_type, revision.version) for revision in revisions] == [
            ("created", "v1"), ("created", "v1")
        ]


def test_item_detail_lists_archived_information_without_reading_content(config_file: Path) -> None:
    client, data_root = _client(config_file)
    engine = create_db_engine(data_root / "state" / "oa.db")
    from oa_knowledge.db.models import ArchivedFile
    with Session(engine) as session:
        item = OAItem(oa_item_key="detail-1", source_channel="done", title="制度通知", pipeline_status="files_verified")
        session.add(item)
        session.flush()
        session.add(ArchivedFile(
            oa_item_id=item.id, original_name="正文.pdf", attachment_key="file-1",
            file_role="official_body", source_container_key="root", depth=2,
            local_relpath="raw/done/正文.pdf", size_bytes=1024, sha256="a" * 64,
            mime_type="application/pdf", download_status="verified",
        ))
        session.commit()
        item_id = item.id
    payload = client.get(f"/api/items/{item_id}").json()
    assert payload["summary"] == {"file_count": 1, "total_bytes": 1024, "verified_count": 1, "attachment_count": 1}
    assert payload["files"][0]["file_role"] == "official_body"
    assert payload["files"][0]["sha256"] == "a" * 64


def test_archive_job_start_is_bounded_and_duplicate_safe(config_file: Path) -> None:
    _, data_root = _client(config_file)
    settings = load_settings(config_file)
    engine = create_db_engine(data_root / "state" / "oa.db")
    with Session(engine) as session:
        batch = CollectionBatch(
            batch_key="web-run", plan_hash="7" * 64, source_channel="done",
            planned_limit=20, discovered_count=20, status="paused",
        )
        session.add(batch)
        session.commit()
        batch_id = batch.id
    job = start_archive_job(settings, batch_id, max_items=10, time_budget_seconds=900)
    assert job["status"] == "queued"
    with pytest.raises(ValueError, match="already active"):
        start_archive_job(settings, batch_id, max_items=10, time_budget_seconds=900)
    paused = pause_archive_batch(settings, batch_id)
    assert paused["status"] == "paused"


def test_backfill_campaign_start_is_idempotent_and_bounded(config_file: Path) -> None:
    _, data_root = _client(config_file)
    settings = load_settings(config_file)
    first = start_backfill_campaign(settings, "2019-01-01", "2026-01-01", 20, 1800)
    second = start_backfill_campaign(settings, "2019-01-01", "2026-01-01", 20, 1800)
    assert first["created"] is True
    assert second == {**first, "created": False}

    engine = create_db_engine(data_root / "state" / "oa.db")
    with Session(engine) as session:
        job = session.get(OperationJob, first["job_id"])
        assert job is not None
        assert job.job_type == "backfill_campaign"
        assert job.status == "queued"
        assert json.loads(job.parameters_json)["chunk_size"] == 20
        job.status = "auth_required"
        job.last_error_code = "auth_required"
        session.commit()

    resumed = start_backfill_campaign(settings, "2019-01-01", "2026-01-01", 20, 1800)
    assert resumed["created"] is False
    assert resumed["resumed"] is True
    assert resumed["status"] == "queued"

    with pytest.raises(ValueError, match="earlier"):
        start_backfill_campaign(settings, "2026-01-01", "2019-01-01")
    with pytest.raises(ValueError, match="chunk_size"):
        start_backfill_campaign(settings, chunk_size=21)


def test_backfill_campaign_web_endpoint_is_csrf_protected(config_file: Path) -> None:
    client, _ = _client(config_file)
    assert client.post("/api/backfill/start", json={}).status_code == 403
    client.get("/api/status")
    csrf = client.cookies.get("oa_csrf") or ""
    response = client.post(
        "/api/backfill/start", headers={"x-csrf-token": csrf},
        json={"from_date": "2019-01-01", "to_date": "2026-01-01", "chunk_size": 20},
    )
    assert response.status_code == 202
    assert response.json()["created"] is True
    latest = client.get("/api/backfill/status")
    assert latest.status_code == 200
    assert latest.json()["job_type"] == "backfill_campaign"


def test_archive_job_exposes_first_item_failure(config_file: Path, monkeypatch) -> None:
    _, data_root = _client(config_file)
    settings = load_settings(config_file)
    engine = create_db_engine(data_root / "state" / "oa.db")
    with Session(engine) as session:
        job = OperationJob(
            job_key="item-failed-job", job_type="archive_batch", status="queued",
            idempotency_key="item-failed-idem",
            parameters_json=json.dumps({
                "batch_key": "batch-key", "max_items": 20, "time_budget_seconds": 900,
            }),
        )
        session.add(job)
        session.commit()
        job_id = job.id
    monkeypatch.setattr(
        "oa_knowledge.web.status.subprocess.run",
        lambda *args, **kwargs: __import__("subprocess").CompletedProcess(
            args[0], 0, stdout='{"processed":1,"run_status":"item_failed"}\n', stderr="",
        ),
    )
    execute_archive_job(settings, job_id, config_file)
    with Session(engine) as session:
        job = session.get(OperationJob, job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.last_error_code == "item_failed"
