"""Console API tests for the product-aligned WebUI (plan-0807-1 §4, §7, §8, §13.6-§13.7)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import (
    ArchivedFile, ItemOccurrence, LogicalItem, MarkdownExport, OAItem, OAManifestItem, ParseArtifact,
    NotificationDelivery, PipelineTask,
)


def _client(config_file: Path) -> TestClient:
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    upgrade_database(settings.database_path)
    from oa_knowledge.web import create_web_app
    return TestClient(create_web_app(settings, config_path=config_file))


def test_web_runtime_secrets_stay_outside_data(config_file: Path) -> None:
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    upgrade_database(settings.database_path)
    from oa_knowledge.web import create_web_app

    create_web_app(settings, config_path=config_file)

    assert (settings.runtime_root / "web-session.key").is_file()
    assert (settings.runtime_root / "web-bootstrap.token").is_file()
    assert not (settings.data_root / "runtime").exists()


def test_dashboard_reports_three_chains(config_file: Path) -> None:
    client = _client(config_file)
    resp = client.get("/api/dashboard")
    assert resp.status_code == 200
    payload = resp.json()
    for chain in ("pending_notification", "done_archive", "markdown_delivery"):
        assert chain in payload
        assert payload[chain]["status"] in {"normal", "abnormal"}
    assert isinstance(payload["needs_attention"], list)


def test_dashboard_surfaces_a_live_oa_login_activity_without_oa_content(config_file: Path) -> None:
    """Regression: the overview must expose a current login state, not only past scans."""
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    runtime = settings.runtime_root
    runtime.mkdir(parents=True)
    (runtime / "operation-worker.json").write_text(json.dumps({
        "owner": "worker-synthetic", "pid": 1, "status": "logging_in",
        "activity": "正在验证 OA 登录", "heartbeat_at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")

    payload = _client(config_file).get("/api/dashboard").json()

    assert payload["oa_activity"]["status"] == "logging_in"
    assert payload["oa_activity"]["label"] == "正在登录 OA"
    assert payload["oa_activity"]["detail"] == "正在验证 OA 登录"
    assert "title" not in payload["oa_activity"]


def test_dashboard_reports_that_oa_session_is_closed_between_scheduled_tasks(config_file: Path) -> None:
    """The overview must not present an idle worker as an active OA session."""
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    runtime = settings.runtime_root
    runtime.mkdir(parents=True)
    (runtime / "operation-worker.json").write_text(json.dumps({
        "owner": "worker-synthetic", "pid": 1, "status": "idle",
        "activity": "当前未登录 OA，等待下次定时任务",
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")

    payload = _client(config_file).get("/api/dashboard").json()

    assert payload["oa_activity"]["status"] == "disconnected"
    assert payload["oa_activity"]["label"] == "OA 已退出"
    assert payload["oa_activity"]["detail"] == "当前未登录 OA，等待下次定时任务"


def test_dashboard_markdown_delivery_counts_success(config_file: Path) -> None:
    # Regression: the conversion pipeline stores delivered files with
    # status "success"; the dashboard must count that value, not the display
    # label "exported" (which is never written to the DB).
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        oa_item = OAItem(oa_item_key="oa:md-count", source_channel="done", title="X", pipeline_status="archived")
        session.add(oa_item); session.flush()
        archived = ArchivedFile(
            oa_item_id=oa_item.id, original_name="报告.pdf", attachment_key="att-1",
            file_role="primary", source_container_key="c1",
        )
        session.add(archived); session.flush()
        session.add(MarkdownExport(
            source_file_id=archived.id,
            source_sha256="0" * 64, source_relpath="报告.pdf", markdown_relpath="报告.pdf.md",
            parse_engine="mineru", parse_engine_version="v1", parse_config_hash="cfg",
            schema_version=1, status="success", generated_at=datetime.now(timezone.utc),
        ))
        session.commit()

    client = _client(config_file)
    resp = client.get("/api/dashboard")
    assert resp.status_code == 200
    md = resp.json()["markdown_delivery"]
    assert md["exported"] == 1
    assert md["pending"] == 0
    assert md["failed"] == 0
    assert md["status"] == "normal"


def test_pending_notifications_list_and_detail(config_file: Path) -> None:
    client = _client(config_file)
    engine = create_db_engine(load_settings(config_file).database_path)
    with Session(engine) as session:
        logical = LogicalItem(logical_key="pend:x", title="X", lifecycle_status="pending")
        session.add(logical)
        session.flush()
        session.add(ItemOccurrence(
            logical_item_id=logical.id, occurrence_key="pending:x", channel="pending",
            title="标题", sender="李四", current_node="节点", occurrence_status="active",
        ))
        session.commit()

    listing = client.get("/api/pending-notifications")
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert len(items) == 1
    occ_id = items[0]["id"]
    assert items[0]["feishu_status"] in {"pending", "sent", "failed"}
    assert items[0]["cleanup_status"] in {"not_eligible", "pending_cleanup"}

    detail = client.get(f"/api/pending-notifications/{occ_id}")
    assert detail.status_code == 200
    assert detail.json()["id"] == occ_id


def test_pending_detail_exposes_six_durable_pipeline_stages(config_file: Path) -> None:
    client = _client(config_file)
    engine = create_db_engine(load_settings(config_file).database_path)
    with Session(engine) as session:
        logical = LogicalItem(logical_key="pend:stages", title="Synthetic", lifecycle_status="pending")
        session.add(logical)
        session.flush()
        occurrence = ItemOccurrence(
            logical_item_id=logical.id, occurrence_key="pending:stages", channel="pending",
            title="Synthetic", occurrence_status="active",
        )
        session.add(occurrence)
        session.add(PipelineTask(
            queue_name="realtime_pending", priority=10,
            logical_item_key=str(logical.id), logical_item_id=logical.id,
            stage="pending_parse", status="running", idempotency_key="pending-six-stages",
        ))
        session.commit()
        occurrence_id = occurrence.id

    stages = client.get(f"/api/pending-notifications/{occurrence_id}").json()["stages"]

    assert stages == {
        "discovery": "done",
        "download": "done",
        "markdown": "running",
        "summary": "pending",
        "feishu": "pending",
        "cleanup": "pending",
    }


def test_unknown_delivery_requires_reconciliation_and_rejects_plain_retry(config_file: Path) -> None:
    client = _client(config_file)
    engine = create_db_engine(load_settings(config_file).database_path)
    with Session(engine) as session:
        logical = LogicalItem(logical_key="pend:unknown", title="Synthetic", lifecycle_status="pending")
        session.add(logical); session.flush()
        occurrence = ItemOccurrence(
            logical_item_id=logical.id, occurrence_key="pending:unknown", channel="pending",
            title="Synthetic", occurrence_status="active",
        )
        session.add(occurrence)
        session.add(NotificationDelivery(
            logical_item_id=logical.id, channel="feishu", notification_type="pending_summary",
            idempotency_key="synthetic-unknown-delivery", status="unknown", attempts=1,
        ))
        session.commit()
        occurrence_id = occurrence.id

    detail = client.get(f"/api/pending-notifications/{occurrence_id}")
    retry = client.post(
        f"/api/pending-notifications/{occurrence_id}/retry-delivery",
        headers={"x-csrf-token": dict(client.cookies)["oa_csrf"]},
    )

    assert detail.status_code == 200
    assert detail.json()["can_retry_delivery"] is False
    assert detail.json()["requires_delivery_reconciliation"] is True
    assert detail.json()["can_cleanup"] is False
    assert retry.status_code == 409
    with Session(engine) as session:
        delivery = session.scalar(select(NotificationDelivery))
        assert delivery.status == "unknown"
        assert delivery.attempts == 1


def test_settings_read_and_write_cleanup_policy(config_file: Path) -> None:
    client = _client(config_file)
    get = client.get("/api/settings")
    assert get.status_code == 200
    assert get.json()["data_cleanup"]["auto_cleanup_after_success"] is True

    patch = client.patch(
        "/api/settings", json={"pending_cleanup": {"auto_cleanup_after_success": False}},
        headers={"x-csrf-token": dict(client.cookies)["oa_csrf"]},
    )
    assert patch.status_code == 200
    assert patch.json()["data_cleanup"]["auto_cleanup_after_success"] is False
    assert patch.json().get("restart_required") is True

    # config file on disk reflects the change
    raw = load_settings(config_file).pending_cleanup.auto_cleanup_after_success
    assert raw is False


def test_done_archives_endpoint_returns_status_labels(config_file: Path) -> None:
    client = _client(config_file)
    resp = client.get("/api/done-archives")
    assert resp.status_code == 200
    assert "items" in resp.json()


def test_done_archives_exposes_six_durable_pipeline_stages(config_file: Path) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        session.add(OAManifestItem(
            oa_item_key="oa:stages", title="Synthetic stages", list_page=1,
            processing_status="downloaded",
        ))
        session.add(OAItem(
            oa_item_key="oa:stages", source_channel="done", title="Synthetic stages",
            pipeline_status="archived",
        ))
        session.add(PipelineTask(
            queue_name="historical_done_backfill", priority=30,
            logical_item_key="oa:stages", stage="source_publish", status="running",
            idempotency_key="synthetic-six-stages",
        ))
        session.commit()

    item = _client(config_file).get("/api/done-archives").json()["items"][0]

    assert item["stages"] == {
        "discovery": "done",
        "download": "done",
        "verification": "done",
        "markdown": "running",
        "classification": "pending",
        "publication": "pending",
    }


def test_markdown_outputs_endpoint(config_file: Path) -> None:
    client = _client(config_file)
    resp = client.get("/api/markdown-outputs")
    assert resp.status_code == 200
    assert "documents" in resp.json()


def test_markdown_outputs_endpoint_returns_only_the_requested_page(config_file: Path) -> None:
    """Regression: the knowledge screen must not download every Markdown row."""
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        for index in range(3):
            session.add(MarkdownExport(
                source_sha256=f"{index:064x}", source_relpath=f"source-{index}.pdf",
                markdown_relpath=f"source-{index}.pdf.md", parse_engine="markitdown",
                parse_engine_version="v1", parse_config_hash="cfg", schema_version=1,
                status="success", generated_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            ))
        session.commit()

    payload = _client(config_file).get("/api/markdown-outputs?page=2&page_size=2").json()

    assert payload["total"] == 3
    assert payload["page"] == 2
    assert payload["page_size"] == 2
    assert len(payload["documents"]) == 1


def test_markdown_outputs_resolves_source_item(config_file: Path) -> None:
    """§8: an export linked to an archived file must surface its source OA item title."""
    from datetime import datetime, timezone

    from oa_knowledge.db.engine import create_db_engine
    from sqlalchemy.orm import Session

    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        oa_item = OAItem(oa_item_key="oa:md-test", source_channel="done", title="来源事项X", pipeline_status="archived")
        session.add(oa_item); session.flush()
        archived = ArchivedFile(
            oa_item_id=oa_item.id, original_name="报告.pdf", attachment_key="att-1",
            file_role="primary", source_container_key="c1",
        )
        session.add(archived); session.flush()
        manifest = OAManifestItem(oa_item_key="oa:md-test", title="来源事项X", list_page=1, processing_status="downloaded")
        session.add(manifest)
        export = MarkdownExport(
            source_file_id=archived.id,
            source_sha256="0" * 64, source_relpath="报告.pdf", markdown_relpath="报告.pdf.md",
            parse_engine="mineru", parse_engine_version="v1", parse_config_hash="cfg",
            schema_version=1, status="success", generated_at=datetime.now(timezone.utc),
        )
        session.add(export)
        session.commit()

    client = _client(config_file)
    resp = client.get("/api/markdown-outputs")
    assert resp.status_code == 200
    docs = resp.json()["documents"]
    assert any(d["source_oa_item"] == "来源事项X" and d["source_file"] == "报告.pdf" for d in docs)


def test_markdown_outputs_exposes_v2_item_aggregation(config_file: Path) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = OAItem(
            oa_item_key="oa:item-output", source_channel="done", title="事项级输出",
            archive_relpath="archive/raw/oa/done/synthetic/item-output", source_type="internal",
            internal_category="经营管理", classification_version="v1",
        )
        session.add(item); session.flush()
        session.add(MarkdownExport(
            oa_item_id=item.id, document_kind="item_index", source_sha256="a" * 64,
            source_relpath=item.archive_relpath, markdown_relpath="source/done/synthetic/item-output/_index.md",
            parse_engine="item_index", parse_engine_version="v1", parse_config_hash="v1",
            schema_version=1, status="success", generated_at=datetime.now(timezone.utc),
        ))
        session.commit()

    payload = _client(config_file).get("/api/markdown-outputs").json()
    row = next(row for row in payload["items"] if row["title"] == "事项级输出")
    assert row["source_type"] == "internal"
    assert row["internal_category"] == "经营管理"
    assert row["delivery_status"] == "已交付"
    assert row["index_relpath"].endswith("/_index.md")
