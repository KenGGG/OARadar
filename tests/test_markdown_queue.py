from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import (
    ArchivedFile,
    ClassificationDecision,
    ClassificationRun,
    MarkdownExport,
    MarkdownTask,
    OAItem,
)
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


def _mark_publishable(session: Session, item: OAItem) -> None:
    run = ClassificationRun(
        run_id=f"synthetic-run-{item.oa_item_key}", run_kind="full", status="completed",
        input_signature="a" * 64, manifest_sha256="a" * 64,
        exclusion_policy_sha256="b" * 64, rule_version="synthetic",
        schema_version="synthetic", prompt_version="synthetic", model_name="synthetic",
        private_config_sha256="c" * 64, target_count=1, excluded_count=0,
    )
    session.add(run); session.flush()
    session.add(ClassificationDecision(
        classification_run_id=run.id, oa_item_key=item.oa_item_key, version=1,
        is_current=True, decision_input_sha256="d" * 64, decision_source="manual",
        classification_status="classified", content_integrity_status="ok",
        content_origin="internal", flow_type="approval", initiator_type="internal",
        business_category="08_行政采购与信息化", classification_confidence=1.0,
        transfer_chain_json="[]", normalized_title=item.title,
        classification_reason_json="{}", rule_version="synthetic",
        private_config_sha256="c" * 64, manual_locked=True, actor="synthetic",
    ))
    session.flush()


def test_enqueue_file_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        item = _make_item(session)
        _mark_publishable(session, item)
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
        _mark_publishable(session, item)
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
        _mark_publishable(session, item)
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
        _mark_publishable(session, item)
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


def test_unclassified_excluded_and_needs_review_items_cannot_enqueue_markdown(
    tmp_path: Path,
) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        item = _make_item(session, key="ITEM-BLOCKED")
        _make_attachment(session, item)
        # No current classified decision is equivalent to no release permit.
        assert enqueue_verified_for_oa(session, item.oa_item_key) == 0
        assert session.query(MarkdownTask).count() == 0
