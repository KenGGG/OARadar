"""已办事项单一简化状态 + 服务端筛选分页测试（plan Task 2）。

全部使用合成 / 不可逆假标识。
"""

from __future__ import annotations

from datetime import datetime, timezone
import csv
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, MarkdownExport, OAItem, OAManifestItem
from oa_knowledge.web import create_web_app
from oa_knowledge.web.console_views import _simple_done_state, _markdown_status_for_item


def _client(config_file: Path) -> TestClient:
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    upgrade_database(settings.database_path)
    return TestClient(create_web_app(settings, config_path=config_file))


def _seed_fact(session: Session, key: str, facts: dict) -> OAManifestItem:
    processing_status = facts["manifest"]
    oa_item: OAItem | None = None
    if facts.get("markdown") == "success" or facts.get("index") is not None:
        oa_item = OAItem(oa_item_key=key, source_channel="done", title=f"{key} 标题", pipeline_status=processing_status)
        session.add(oa_item)
        session.flush()

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

    if facts.get("index") is not None and oa_item is not None:
        session.add(MarkdownExport(
            oa_item_id=oa_item.id, document_kind="item_index", source_sha256="1" * 64,
            source_relpath=f"archive/raw/oa/done/{key}", markdown_relpath=f"{key}/_index.md",
            parse_engine="item_index", parse_engine_version="v1", parse_config_hash="v1",
            schema_version=1, status=facts["index"], generated_at=datetime.now(timezone.utc),
        ))

    session.commit()
    return manifest


@pytest.mark.parametrize(("facts", "expected"), [
    ({"manifest": "discovered"}, "waiting_download"),
    ({"manifest": "downloaded", "markdown": "pending"}, "waiting_markdown"),
    ({"manifest": "downloaded", "markdown": "success"}, "waiting_markdown"),
    ({"manifest": "downloaded", "markdown": "success", "index": "success"}, "completed"),
    ({"manifest": "downloaded", "markdown": "success", "index": "failed"}, "attention"),
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
            "index": "success",
        })

    payload = client.get("/api/done-archives?page=1&page_size=50").json()
    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["simple_status"] == "completed"
    assert item["simple_status_label"] == "已完成"
    assert item["attention_reason"] is None
    assert item["updated_at"] is not None


def test_done_archives_keeps_online_oa_page_and_row_order_and_exposes_initiator_fields(config_file: Path) -> None:
    """Changing the OA page/row order must change the user-visible Done order."""
    client = _client(config_file)
    engine = create_db_engine(load_settings(config_file).database_path)
    with Session(engine) as session:
        session.add_all([
            OAManifestItem(
                oa_item_key="oa:page-2", title="第二页第一条", sender="发起人乙",
                initiated_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
                completed_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
                list_page=2, list_ordinal=1, processing_status="discovered",
            ),
            OAManifestItem(
                oa_item_key="oa:page-1-second", title="第一页第二条", sender="发起人甲二",
                initiated_at=datetime(2026, 8, 1, 11, tzinfo=timezone.utc),
                completed_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
                list_page=1, list_ordinal=2, processing_status="discovered",
            ),
            OAManifestItem(
                oa_item_key="oa:page-1-first", title="第一页第一条", sender="发起人甲一",
                initiated_at=datetime(2026, 8, 1, 10, tzinfo=timezone.utc),
                completed_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
                list_page=1, list_ordinal=1, processing_status="discovered",
            ),
        ])
        session.commit()

    payload = client.get("/api/done-archives?page=1&page_size=50&simple_status=waiting_download").json()
    assert [item["title"] for item in payload["items"]] == ["第一页第一条", "第一页第二条", "第二页第一条"]
    assert payload["items"][0]["sender"] == "发起人甲一"
    assert payload["items"][0]["initiated_at"] == "2026-08-01T10:00:00"


def test_done_archives_counts_files_present_in_originals_directory(config_file: Path) -> None:
    """The displayed attachment count must match the files a user sees in originals."""
    client = _client(config_file)
    settings = load_settings(config_file)
    archive_relpath = "originals/2026/08/2026-08-23_合成事项"
    archive_dir = settings.data_root / archive_relpath
    archive_dir.mkdir(parents=True)
    for name in ("附件甲.pdf", "正文乙.docx", "附件丙.xlsx"):
        (archive_dir / name).write_bytes(b"synthetic")

    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = OAItem(
            oa_item_key="oa:actual-files", source_channel="done", title="合成事项",
            pipeline_status="downloaded", archive_relpath=archive_relpath,
        )
        session.add(item)
        session.flush()
        session.add_all([
            ArchivedFile(
                oa_item_id=item.id, original_name="附件甲.pdf", attachment_key="one",
                file_role="direct_attachment", source_container_key="synthetic",
                local_relpath=f"{archive_relpath}/附件甲.pdf", download_status="verified",
            ),
            ArchivedFile(
                oa_item_id=item.id, original_name="正文乙.docx", attachment_key="two",
                file_role="official_body", source_container_key="synthetic",
                local_relpath=f"{archive_relpath}/正文乙.docx", download_status="verified",
            ),
        ])
        session.add(OAManifestItem(
            oa_item_key="oa:actual-files", title="合成事项", list_page=1,
            processing_status="downloaded", archive_relpath=archive_relpath,
        ))
        session.add(OAManifestItem(
            oa_item_key="oa:actual-files-duplicate", title="合成事项重复记录", list_page=1,
            list_ordinal=2, processing_status="downloaded", archive_relpath=archive_relpath,
        ))
        session.commit()

    payload = client.get("/api/done-archives?page=1&page_size=50").json()
    assert payload["items"][0]["file_count"] == 3
    assert payload["items"][0]["attachment_names"] == ["正文乙.docx", "附件丙.xlsx", "附件甲.pdf"]
    assert payload["metrics"]["verified_attachments"] == 3


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
                "index": "success",
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


def test_done_archives_filters_confirmed_no_attachment_for_manual_review(config_file: Path) -> None:
    """Removing the dedicated review filter would mix zero attachments with other rows."""
    client = _client(config_file)
    engine = create_db_engine(load_settings(config_file).database_path)
    with Session(engine) as session:
        _seed_fact(session, "oa:none", {"manifest": "no_attachment"})
        _seed_fact(session, "oa:pending", {"manifest": "discovered"})
        _seed_fact(session, "oa:excluded", {"manifest": "skipped"})

    payload = client.get(
        "/api/done-archives?page=1&page_size=50&attachment_review=no_attachment"
    ).json()

    assert payload["total"] == 1
    assert payload["items"][0]["item_id"] is None
    assert payload["items"][0]["pipeline_status"] == "no_attachment"
    assert payload["items"][0]["file_count"] == 0
    assert payload["items"][0]["attachment_review_label"] == "0（待人工复核）"


def test_done_archives_marks_no_attachment_for_review_even_before_filtering(config_file: Path) -> None:
    """The review cue must remain visible in the unfiltered Done list."""
    client = _client(config_file)
    engine = create_db_engine(load_settings(config_file).database_path)
    with Session(engine) as session:
        _seed_fact(session, "oa:none-unfiltered", {"manifest": "no_attachment"})

    item = client.get("/api/done-archives?page=1&page_size=50").json()["items"][0]

    assert item["file_count"] == 0
    assert item["attachment_review_label"] == "0（待人工复核）"


def test_done_archives_csv_exports_all_filtered_rows_not_only_first_page(config_file: Path) -> None:
    """CSV export must honor active filters while ignoring UI pagination."""
    client = _client(config_file)
    engine = create_db_engine(load_settings(config_file).database_path)
    with Session(engine) as session:
        for index in range(55):
            _seed_fact(session, f"oa:none-{index}", {"manifest": "no_attachment"})
        _seed_fact(session, "oa:other", {"manifest": "discovered"})

    response = client.get("/api/done-archives/export.csv?attachment_review=no_attachment")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment;" in response.headers["content-disposition"]
    assert response.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert len(rows) == 55
    assert set(row["附件数量"] for row in rows) == {"0"}
    assert set(row["状态"] for row in rows) == {"确认无附件"}


def test_done_archives_csv_without_filters_exports_every_row(config_file: Path) -> None:
    """An unfiltered export is the full Done list, not the current page."""
    client = _client(config_file)
    engine = create_db_engine(load_settings(config_file).database_path)
    with Session(engine) as session:
        _seed_fact(session, "oa:all-one", {"manifest": "no_attachment"})
        _seed_fact(session, "oa:all-two", {"manifest": "discovered"})

    response = client.get("/api/done-archives/export.csv")
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))

    assert len(rows) == 2
    assert {row["标题"] for row in rows} == {"oa:all-one 标题", "oa:all-two 标题"}
