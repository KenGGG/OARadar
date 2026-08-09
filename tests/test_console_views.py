"""Console API tests for the product-aligned WebUI (plan-0807-1 §4, §7, §8, §13.6-§13.7)."""

from __future__ import annotations

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
)


def _client(config_file: Path) -> TestClient:
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    upgrade_database(settings.database_path)
    from oa_knowledge.web import create_web_app
    return TestClient(create_web_app(settings, config_path=config_file))


def test_dashboard_reports_three_chains(config_file: Path) -> None:
    client = _client(config_file)
    resp = client.get("/api/dashboard")
    assert resp.status_code == 200
    payload = resp.json()
    for chain in ("pending_notification", "done_archive", "markdown_delivery"):
        assert chain in payload
        assert payload[chain]["status"] in {"normal", "abnormal"}
    assert isinstance(payload["needs_attention"], list)


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


def test_markdown_outputs_endpoint(config_file: Path) -> None:
    client = _client(config_file)
    resp = client.get("/api/markdown-outputs")
    assert resp.status_code == 200
    assert "documents" in resp.json()


def test_maintenance_endpoint(config_file: Path) -> None:
    client = _client(config_file)
    resp = client.get("/api/maintenance")
    assert resp.status_code == 200
    assert "doctor" in resp.json()


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
