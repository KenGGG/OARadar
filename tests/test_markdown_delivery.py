"""V2 Markdown Delivery classification and item-index tests."""

import sqlite3

from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import MarkdownExport, OAItem
from oa_knowledge.markdown_delivery import classify_done_item, publish_item_index


def _item(session, *, title: str, sender: str | None, document_number: str | None = None) -> OAItem:
    item = OAItem(
        oa_item_key="done:classify", source_channel="done", title=title,
        sender=sender, document_number=document_number, pipeline_status="files_verified",
    )
    session.add(item)
    session.flush()
    return item


def test_internal_classification_updates_only_current_four_fields(tmp_path) -> None:
    settings = Settings(app={"data_root": tmp_path / "data"})
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = _item(session, title="关于预算资金管理的内部通知", sender="本公司财务部")
        classify_done_item(session, item.oa_item_key)

        assert item.source_type == "internal"
        assert item.internal_category == "财务资金"
        assert item.external_issuer is None
        assert item.classification_version == "v2-rules"


def test_external_classification_uses_sender_as_normalized_issuer(tmp_path) -> None:
    settings = Settings(app={"data_root": tmp_path / "data"})
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = _item(
            session, title="关于开展专项检查的通知", sender="示例省国资委",
            document_number="示例国资〔2026〕1号",
        )
        classify_done_item(session, item.oa_item_key)

        assert item.source_type == "external"
        assert item.internal_category is None
        assert item.external_issuer == "示例省国资委"
        assert item.classification_version == "v2-rules"


def test_item_index_ledger_migration_enforces_document_kind_and_unique_item_schema(tmp_path) -> None:
    settings = Settings(app={"data_root": tmp_path / "data"})
    upgrade_database(settings.database_path)

    with sqlite3.connect(settings.database_path) as connection:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'markdown_exports'"
        ).fetchone()[0]
        indexes = connection.execute("PRAGMA index_list('markdown_exports')").fetchall()

    assert "ck_markdown_export_document_kind" in table_sql
    assert any(index[1] == "uq_markdown_export_item_index_schema" for index in indexes)


def test_no_attachment_item_publishes_stable_index_without_moving_archive_path(tmp_path) -> None:
    settings = Settings(app={"data_root": tmp_path / "data"})
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = _item(session, title="内部工作会议", sender="本公司综合部")
        item.archive_relpath = "originals/done/synthetic/item"
        classify_done_item(session, item.oa_item_key)
        first = publish_item_index(session, settings, item.oa_item_key)
        first_mtime = first.stat().st_mtime_ns
        second = publish_item_index(session, settings, item.oa_item_key)
        exports = session.query(MarkdownExport).all()

    assert first == second
    assert second.stat().st_mtime_ns == first_mtime
    content = second.read_text(encoding="utf-8")
    assert "source_type: \"internal\"" in content
    assert "无附件" in content
    assert len(exports) == 1
    assert exports[0].oa_item_id == item.id
    assert exports[0].document_kind == "item_index"
    assert exports[0].status == "success"
    assert exports[0].source_file_id is None
