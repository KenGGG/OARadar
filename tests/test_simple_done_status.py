"""已办事项单一简化状态 + 服务端筛选分页测试（plan Task 2）。

全部使用合成 / 不可逆假标识。
"""

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
    ArchivedFile, CuratedDecision, CuratedRun, LogicalItem, MarkdownExport, OAItem,
    OAManifestItem, OnlineAuditItem, OnlineAuditRun,
)
from oa_knowledge.web import create_web_app
from oa_knowledge.web.console_views import _simple_done_state, _markdown_status_for_item


def _client(config_file: Path) -> TestClient:
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    upgrade_database(settings.database_path)
    return TestClient(create_web_app(settings, config_path=config_file))


def _seed_fact(session: Session, key: str, facts: dict) -> OAManifestItem:
    processing_status = facts["manifest"]
    depth_limited = processing_status == "depth_limit_reached"
    if depth_limited:
        processing_status = "downloaded"

    oa_item: OAItem | None = None
    logical: LogicalItem | None = None
    if facts.get("markdown") == "success" or facts.get("curation") is not None:
        oa_item = OAItem(oa_item_key=key, source_channel="done", title=f"{key} 标题", pipeline_status=processing_status)
        session.add(oa_item)
        session.flush()
        if facts.get("curation") is not None:
            logical = LogicalItem(logical_key=key, title=f"{key} 标题", lifecycle_status="done_confirmed")
            session.add(logical)
            session.flush()
            oa_item.logical_item_id = logical.id

    manifest = OAManifestItem(
        oa_item_key=key, title=f"{key} 标题", processing_status=processing_status,
        completed_at=datetime.now(timezone.utc), last_synced_at=datetime.now(timezone.utc),
        list_page=1,
    )
    session.add(manifest)
    session.flush()

    if facts.get("markdown") == "success" and oa_item is not None:
        archived = ArchivedFile(
            oa_item_id=oa_item.id, original_name="r.pdf", attachment_key="a",
            file_role="primary", source_container_key="c",
        )
        session.add(archived)
        session.flush()
        session.add(MarkdownExport(
            source_file_id=archived.id, source_sha256="0" * 64, source_relpath=f"{key}.src",
            markdown_relpath=f"{key}.md", parse_engine="mineru", parse_engine_version="v1",
            parse_config_hash="cfg", schema_version=1, status="success",
            generated_at=datetime.now(timezone.utc),
        ))

    if facts.get("curation") is not None and logical is not None:
        curated = CuratedRun(
            logical_item_id=logical.id, input_signature="sig", status=facts["curation"],
            rules_version="v1", prompt_version="v1", schema_version="v1",
            model_name="qwen3.5:9b", config_signature="cs",
        )
        session.add(curated)
        session.flush()
        for ordinal, decision_status in enumerate(facts.get("decisions", [])):
            session.add(CuratedDecision(
                curated_run_id=curated.id, ordinal=ordinal, status=decision_status,
                document_kind="report", normalized_title=f"{key} 标题",
                decision_hash=f"h{ordinal}", confidence=0.9,
            ))

    if depth_limited:
        audit_run = OnlineAuditRun(status="completed", total_items=1, completed_items=1)
        session.add(audit_run)
        session.flush()
        session.add(OnlineAuditItem(
            run_id=audit_run.id, oa_item_key=key, title=f"{key} 标题", status="pending",
            depth_limit_reached=True,
        ))

    session.commit()
    return manifest


@pytest.mark.parametrize(("facts", "expected"), [
    ({"manifest": "discovered"}, "waiting_download"),
    ({"manifest": "downloaded", "markdown": "pending"}, "waiting_markdown"),
    ({"manifest": "downloaded", "markdown": "success", "curation": "queued"}, "waiting_classification"),
    ({"manifest": "downloaded", "markdown": "success", "curation": "completed", "decisions": ["published"]}, "completed"),
    ({"manifest": "downloaded", "markdown": "success", "curation": "needs_review"}, "attention"),
    ({"manifest": "skipped"}, "excluded"),
    ({"manifest": "depth_limit_reached"}, "attention"),
])
def test_done_archive_simple_status_uses_business_priority(
    config_file: Path, facts: dict, expected: str,
) -> None:
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        manifest = _seed_fact(session, "oa:pri", facts)
        reloaded = session.get(OAManifestItem, manifest.id)
        archived = session.scalar(select(OAItem).where(OAItem.oa_item_key == reloaded.oa_item_key))
        md = _markdown_status_for_item(session, {"id": reloaded.id}, reloaded)
        result = _simple_done_state(session, reloaded, archived, md)
        assert result["state"] == expected


def test_done_archives_exposes_simple_status_fields(config_file: Path) -> None:
    client = _client(config_file)
    engine = create_db_engine(load_settings(config_file).database_path)
    with Session(engine) as session:
        _seed_fact(session, "oa:done-1", {
            "manifest": "downloaded", "markdown": "success",
            "curation": "completed", "decisions": ["published"],
        })

    payload = client.get("/api/done-archives?page=1&page_size=50").json()
    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["simple_status"] == "completed"
    assert item["simple_status_label"] == "已完成"
    assert item["attention_reason"] is None
    assert item["updated_at"] is not None


def test_done_archives_filters_by_simple_status_server_side(config_file: Path) -> None:
    client = _client(config_file)
    settings = load_settings(config_file)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        # 60 个待下载 + 60 个已完成。
        for i in range(60):
            _seed_fact(session, f"oa:dl-{i}", {"manifest": "discovered"})
        for i in range(60):
            _seed_fact(session, f"oa:cp-{i}", {
                "manifest": "downloaded", "markdown": "success",
                "curation": "completed", "decisions": ["published"],
            })

    done = client.get("/api/done-archives?page=1&page_size=50&simple_status=waiting_download").json()
    assert done["total"] == 60
    assert len(done["items"]) == 50
    assert all(it["simple_status"] == "waiting_download" for it in done["items"])

    page2 = client.get("/api/done-archives?page=2&page_size=50&simple_status=waiting_download").json()
    assert len(page2["items"]) == 10
    assert all(it["simple_status"] == "waiting_download" for it in page2["items"])

    completed = client.get("/api/done-archives?page=1&page_size=50&simple_status=completed").json()
    assert completed["total"] == 60
    assert all(it["simple_status"] == "completed" for it in completed["items"])


def test_done_archives_rejects_unknown_simple_status(config_file: Path) -> None:
    client = _client(config_file)
    resp = client.get("/api/done-archives?simple_status=bogus")
    assert resp.status_code == 422
