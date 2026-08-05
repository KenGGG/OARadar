from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, MarkdownExport, MarkdownTask, OAItem
from oa_knowledge.markdown_export.render import SCHEMA_VERSION
from oa_knowledge.markdown_queue import enqueue_file, enqueue_verified_for_oa


def _make_item(session: Session, key: str = "ITEM-1") -> OAItem:
    item = OAItem(oa_item_key=key, source_channel="oa", title="synthetic-title")
    session.add(item)
    session.flush()
    return item


def _make_attachment(session: Session, item: OAItem, key: str = "A1") -> ArchivedFile:
    f = ArchivedFile(
        oa_item_id=item.id,
        original_name="doc.pdf",
        attachment_key=key,
        file_role="direct_attachment",
        source_container_key="container",
        download_status="verified",
        local_relpath="raw/done/2024/01/doc.pdf",
    )
    session.add(f)
    session.flush()
    return f


def test_enqueue_file_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        item = _make_item(session)
        f = _make_attachment(session, item)
        assert enqueue_file(session, f.id) is True
        # A second enqueue for the same file is a no-op.
        assert enqueue_file(session, f.id) is False
        assert session.query(MarkdownTask).filter_by(source_file_id=f.id).count() == 1


def test_enqueue_file_skips_when_export_succeeded(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        item = _make_item(session)
        f = _make_attachment(session, item)
        session.add(MarkdownExport(
            source_file_id=f.id,
            source_sha256="0" * 64,
            source_relpath="raw/done/2024/01/doc.pdf",
            markdown_relpath="raw/done/2024/01/doc.pdf.md",
            parse_engine="markitdown",
            parse_engine_version="1",
            parse_config_hash="0" * 64,
            schema_version=SCHEMA_VERSION,
            status="success",
        ))
        session.flush()
        assert enqueue_file(session, f.id) is False


def test_enqueue_verified_for_oa_counts_attachments(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        item = _make_item(session, key="ITEM-X")
        f = _make_attachment(session, item, key="A1")
        # Non-attachment roles must not be enqueued.
        session.add(ArchivedFile(
            oa_item_id=item.id,
            original_name="meta.json",
            attachment_key="M1",
            file_role="metadata_snapshot",
            source_container_key="container",
            download_status="verified",
            local_relpath="raw/done/2024/01/meta.json",
        ))
        session.flush()
        assert enqueue_verified_for_oa(session, "ITEM-X") == 1
        # Idempotent on a second pass.
        assert enqueue_verified_for_oa(session, "ITEM-X") == 0
        assert session.query(MarkdownTask).filter_by(source_file_id=f.id).count() == 1


def test_all_knowledge_source_roles_are_enqueued(tmp_path: Path) -> None:
    from oa_knowledge.source_roles import KNOWLEDGE_SOURCE_ROLES

    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        item = _make_item(session, key="ITEM-ROLES")
        for index, role in enumerate(KNOWLEDGE_SOURCE_ROLES):
            session.add(ArchivedFile(
                oa_item_id=item.id,
                original_name=f"{role}.pdf",
                attachment_key=f"K{index}",
                file_role=role,
                source_container_key="container",
                download_status="verified",
                local_relpath=f"raw/done/2024/01/{role}.pdf",
            ))
        session.flush()
        # metadata_snapshot must still be excluded.
        session.add(ArchivedFile(
            oa_item_id=item.id,
            original_name="meta.json",
            attachment_key="META",
            file_role="metadata_snapshot",
            source_container_key="container",
            download_status="verified",
            local_relpath="raw/done/2024/01/meta.json",
        ))
        session.flush()
        # Every knowledge-source role should be enqueued exactly once.
        assert enqueue_verified_for_oa(session, "ITEM-ROLES") == len(KNOWLEDGE_SOURCE_ROLES)
        enqueued = session.query(MarkdownTask).count()
        assert enqueued == len(KNOWLEDGE_SOURCE_ROLES)
        # metadata_snapshot is never enqueued.
        assert enqueue_verified_for_oa(session, "ITEM-ROLES") == 0
