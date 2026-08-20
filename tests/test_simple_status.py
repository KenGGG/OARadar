"""极简状态聚合接口测试（plan Task 1 & Task 4）。

全部使用合成 / 不可逆假标识，绝不写入真实 OA 正文、附件名或凭据。
"""

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
    ArchivedFile, ItemOccurrence, ItemSnapshot, LogicalItem, MarkdownExport,
    OAItem, OAManifestItem, SummaryVersion,
)
from oa_knowledge.web import create_web_app


def _client(config_file: Path) -> TestClient:
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    upgrade_database(settings.database_path)
    return TestClient(create_web_app(settings, config_path=config_file))


def _seed(config_file: Path):
    settings = load_settings(config_file)
    engine = create_db_engine(settings.database_path)
    return engine


def _add_completed_item(session: Session, key: str, *, depth_limited: bool = False) -> None:
    """已归档 + Source Markdown + V2 事项索引（spec §9.4 已完成）。"""
    oa_item = OAItem(oa_item_key=key, source_channel="done", title=f"{key} 标题", pipeline_status="downloaded")
    session.add(oa_item)
    session.flush()
    manifest = OAManifestItem(
        oa_item_key=key, title=f"{key} 标题", processing_status=("depth_limit_reached" if depth_limited else "downloaded"), list_page=1,
        completed_at=datetime.now(timezone.utc), last_synced_at=datetime.now(timezone.utc),
    )
    session.add(manifest)
    session.flush()
    archived = ArchivedFile(
        oa_item_id=oa_item.id, original_name="报告.pdf", attachment_key="att-1",
        file_role="primary", source_container_key="c1",
    )
    session.add(archived)
    session.flush()
    session.add(MarkdownExport(
        source_file_id=archived.id, source_sha256="0" * 64, source_relpath=f"{key}.src",
        markdown_relpath=f"{key}.md", parse_engine="mineru", parse_engine_version="v1",
        parse_config_hash="cfg", schema_version=1, status="success",
        generated_at=datetime.now(timezone.utc),
    ))
    session.add(MarkdownExport(
        oa_item_id=oa_item.id, document_kind="item_index", source_sha256="1" * 64,
        source_relpath=f"archive/raw/oa/done/{key}", markdown_relpath=f"{key}/_index.md",
        parse_engine="item_index", parse_engine_version="v1", parse_config_hash="v1",
        schema_version=1, status="success", generated_at=datetime.now(timezone.utc),
    ))
    session.commit()


def _add_markdown_only_item(session: Session, key: str) -> None:
    """已下载 + 有效 Markdown，但没有 CuratedRun（不能算发布完成）。"""
    oa_item = OAItem(oa_item_key=key, source_channel="done", title=f"{key} 标题", pipeline_status="downloaded")
    session.add(oa_item)
    session.flush()
    manifest = OAManifestItem(
        oa_item_key=key, title=f"{key} 标题", processing_status="downloaded", list_page=1,
        completed_at=datetime.now(timezone.utc), last_synced_at=datetime.now(timezone.utc),
    )
    session.add(manifest)
    session.flush()
    archived = ArchivedFile(
        oa_item_id=oa_item.id, original_name="报告.pdf", attachment_key="att-1",
        file_role="primary", source_container_key="c1",
    )
    session.add(archived)
    session.flush()
    session.add(MarkdownExport(
        source_file_id=archived.id, source_sha256="0" * 64, source_relpath=f"{key}.src",
        markdown_relpath=f"{key}.md", parse_engine="mineru", parse_engine_version="v1",
        parse_config_hash="cfg", schema_version=1, status="success",
        generated_at=datetime.now(timezone.utc),
    ))
    session.commit()


def _add_pending_summary(session: Session, kind: str, status: str, model_name: str) -> None:
    """写一条待办摘要版本（spec §4.3 模型口径）。"""
    logical = LogicalItem(logical_key=f"sum:{kind}:{model_name}:{status}", title="摘要", lifecycle_status="pending")
    session.add(logical)
    session.flush()
    snapshot = ItemSnapshot(
        logical_item_id=logical.id, occurrence_id=None, snapshot_kind="pending",
        version=1, content_hash="0" * 64, payload_json="{}", is_canonical=True,
    )
    session.add(snapshot)
    session.flush()
    session.add(SummaryVersion(
        logical_item_id=logical.id, snapshot_id=snapshot.id, summary_kind="pending",
        version=1, status=status, input_hash="0" * 64, structured_json="{}",
        provider_name="ollama", model_name=model_name, prompt_version="v1",
    ))
    session.commit()


# ---------------------------------------------------------------------------
# Task 1 Step 1: 隐私与结构
# ---------------------------------------------------------------------------

def test_simple_status_returns_plain_language_business_results_without_oa_content(config_file: Path) -> None:
    client = _client(config_file)
    # 写入一个敏感标题，验证它绝不会出现在聚合接口中。
    engine = _seed(config_file)
    with Session(engine) as session:
        session.add(OAManifestItem(
            oa_item_key="oa:leak", title="synthetic confidential title",
            processing_status="discovered", list_page=1,
        ))
        session.commit()

    payload = client.get("/api/simple-status").json()

    assert set(payload) == {"generated_at", "overall_status", "done", "pending", "oa_activity", "attention"}
    assert set(payload["done"]) >= {
        "status", "headline", "oa_total", "archive_complete", "excluded",
        "no_attachment", "markdown_ready_items", "published_items",
        "queued_items", "running_items", "failed_items", "review_items", "last_scan_at",
    }
    assert set(payload["pending"]) >= {
        "status", "headline", "frequency_text", "last_scan_at", "next_scan_at",
        "oa_pending_count", "model_name", "model_success", "model_fallback",
        "model_failed", "feishu_sent", "feishu_failed", "feishu_unknown",
        "last_feishu_success_at",
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "synthetic confidential title" not in serialized
    assert "payload_json" not in serialized
    assert "structured_json" not in serialized


# ---------------------------------------------------------------------------
# Task 1 Step 5: 业务口径
# ---------------------------------------------------------------------------

def test_simple_status_does_not_count_markdown_as_final_publication(config_file: Path) -> None:
    client = _client(config_file)
    engine = _seed(config_file)
    with Session(engine) as session:
        _add_markdown_only_item(session, "oa:md-only")

    done = client.get("/api/simple-status").json()["done"]
    assert done["markdown_ready_items"] >= 1
    assert done["published_items"] == 0
    # 该事项应停留在“等待归类”，而非“已完成”。
    assert done["status"] != "completed"


def test_simple_status_completes_done_item_from_item_index_without_curation(config_file: Path) -> None:
    client = _client(config_file)
    engine = _seed(config_file)
    with Session(engine) as session:
        _add_markdown_only_item(session, "oa:index-complete")
        item = session.scalar(select(OAItem).where(OAItem.oa_item_key == "oa:index-complete"))
        session.add(MarkdownExport(
            oa_item_id=item.id, document_kind="item_index", source_sha256="2" * 64,
            source_relpath="archive/raw/oa/done/synthetic", markdown_relpath="source/done/synthetic/_index.md",
            parse_engine="item_index", parse_engine_version="v1", parse_config_hash="v1",
            schema_version=1, status="success", generated_at=datetime.now(timezone.utc),
        ))
        session.commit()

    done = client.get("/api/simple-status").json()["done"]
    assert done["published_items"] == 1
    assert done["status"] == "completed"


def test_simple_status_keeps_items_without_an_index_in_markdown_queue(config_file: Path) -> None:
    client = _client(config_file)
    engine = _seed(config_file)
    with Session(engine) as session:
        _add_completed_item(session, "oa:complete")
        # Source Markdown 存在但事项索引未发布，不能计入 V2 交付完成。
        oa_item = OAItem(oa_item_key="oa:partial", source_channel="done", title="partial", pipeline_status="downloaded")
        session.add(oa_item)
        session.flush()
        session.add(OAManifestItem(
            oa_item_key="oa:partial", title="partial", processing_status="downloaded", list_page=1,
            completed_at=datetime.now(timezone.utc), last_synced_at=datetime.now(timezone.utc),
        ))
        session.flush()
        archived = ArchivedFile(oa_item_id=oa_item.id, original_name="r.pdf", attachment_key="a", file_role="primary", source_container_key="c")
        session.add(archived)
        session.flush()
        session.add(MarkdownExport(
            source_file_id=archived.id, source_sha256="1" * 64, source_relpath="oa:partial.src",
            markdown_relpath="oa:partial.md", parse_engine="mineru", parse_engine_version="v1",
            parse_config_hash="cfg", schema_version=1, status="success",
            generated_at=datetime.now(timezone.utc),
        ))
        session.commit()

    done = client.get("/api/simple-status").json()["done"]
    assert done["published_items"] == 1
    assert done["oa_total"] == 2
    assert done["queued_items"] >= 1


def test_simple_status_distinguishes_qwen_success_fallback_and_failure(config_file: Path) -> None:
    client = _client(config_file)
    engine = _seed(config_file)
    with Session(engine) as session:
        _add_pending_summary(session, "ok", "current", "qwen3.5:9b")
        _add_pending_summary(session, "fb", "current", "deterministic-fallback")
        _add_pending_summary(session, "fail", "failed", "qwen3.5:9b")

    pending = client.get("/api/simple-status").json()["pending"]
    assert pending["model_success"] == 1
    assert pending["model_fallback"] == 1
    assert pending["model_failed"] == 1
    # 兜底不能计入 qwen 成功数。
    assert pending["model_success"] == 1


def test_simple_status_reports_missing_schedule_time_as_unknown_not_zero(config_file: Path) -> None:
    client = _client(config_file)
    payload = client.get("/api/simple-status").json()
    # 测试环境没有调度运行记录，时间应为 null，绝不能是 0 或 "0"。
    assert payload["done"]["last_scan_at"] is None
    assert payload["pending"]["next_scan_at"] is None


def test_simple_status_never_marks_depth_limit_reached_complete(config_file: Path) -> None:
    client = _client(config_file)
    engine = _seed(config_file)
    with Session(engine) as session:
        _add_completed_item(session, "oa:deep", depth_limited=True)

    payload = client.get("/api/simple-status").json()
    done = payload["done"]
    # 即使原件、Markdown 与发布都具备，容器层级超限也必须归为“需要处理”。
    assert done["published_items"] == 0
    assert done["failed_items"] + done["review_items"] >= 1
    # 该事项的简化状态应为 attention。
    engine2 = _seed(config_file)
    with Session(engine2) as session:
        mid = session.scalar(select(OAManifestItem.id).where(OAManifestItem.oa_item_key == "oa:deep"))
        from oa_knowledge.web.simple_status import _done_simple_status_map
        state = _done_simple_status_map(session)[mid][0]
        assert state == "attention"


# ---------------------------------------------------------------------------
# Task 4: 大白话文案
# ---------------------------------------------------------------------------

def test_simple_status_headline_all_complete(config_file: Path) -> None:
    client = _client(config_file)
    engine = _seed(config_file)
    with Session(engine) as session:
        _add_completed_item(session, "oa:c1")
        _add_completed_item(session, "oa:c2")
    done = client.get("/api/simple-status").json()["done"]
    assert done["status"] == "completed"
    assert "已完成" in done["headline"]
    assert "english" not in done["headline"].lower()


def test_simple_status_headline_still_in_progress(config_file: Path) -> None:
    client = _client(config_file)
    engine = _seed(config_file)
    with Session(engine) as session:
        _add_markdown_only_item(session, "oa:wip")  # 已下载 + MD，但无归类
    done = client.get("/api/simple-status").json()["done"]
    assert done["status"] == "working"
    assert "仍在排队" in done["headline"]


def test_simple_status_headline_fallback_used(config_file: Path) -> None:
    client = _client(config_file)
    engine = _seed(config_file)
    with Session(engine) as session:
        _add_pending_summary(session, "fb", "current", "deterministic-fallback")
    pending = client.get("/api/simple-status").json()["pending"]
    assert pending["status"] == "fallback_used"
    assert "兜底" in pending["headline"]


def test_simple_status_headline_attention_on_failure(config_file: Path) -> None:
    client = _client(config_file)
    engine = _seed(config_file)
    with Session(engine) as session:
        logical = LogicalItem(logical_key="occ:fail", title="失败事项", lifecycle_status="pending")
        session.add(logical)
        session.flush()
        session.add(ItemOccurrence(
            channel="pending", occurrence_status="active", logical_item_id=logical.id,
            occurrence_key="occ:fail", title="失败事项",
        ))
        session.flush()
        from oa_knowledge.db.models import NotificationDelivery
        session.add(NotificationDelivery(
            channel="feishu", notification_type="pending_summary", status="failed",
            idempotency_key="deliv:fail",
        ))
        session.commit()
    pending = client.get("/api/simple-status").json()["pending"]
    assert pending["status"] == "attention"
    assert "需要处理" in pending["headline"]


def test_simple_status_oa_activity_unknown_when_no_worker_heartbeat(config_file: Path) -> None:
    client = _client(config_file)
    oa = client.get("/api/simple-status").json()["oa_activity"]
    assert oa["status"] == "unknown"
    assert "title" not in oa
