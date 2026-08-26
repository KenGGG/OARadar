import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from oa_knowledge.db.engine import create_db_engine, session_scope
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import (
    ArchivedFile,
    ArchiveMember,
    ArchivePackage,
    BatchItem,
    CollectionBatch,
    ContentObject,
    KnowledgeDocument,
    ItemOccurrence,
    ItemSnapshot,
    LogicalItem,
    LlmRequestAudit,
    OAItem,
    OAItemDocumentRelation,
    ParseArtifact,
    ResourceLease,
    SourceReference,
    SourceAttachment,
    SummaryEvidence,
    SummaryJob,
    SummaryVersion,
)


def test_migration_is_idempotent_and_wal_enabled(tmp_path: Path) -> None:
    db = tmp_path / "state" / "oa.db"
    upgrade_database(db)
    upgrade_database(db)
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0038_oa_markdown_v1_classification"
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "oa_items", "files", "runs", "collection_batches", "batch_items", "review_queue",
        "parse_artifacts", "parse_jobs", "content_objects", "knowledge_documents", "source_references",
        "logical_items", "item_occurrences", "item_snapshots", "summary_jobs", "summary_versions",
        "summary_evidence", "resource_leases", "llm_request_audits",
        "source_attachments", "archive_packages", "archive_members", "oa_item_document_relations",
        "curated_runs", "curated_decisions", "curated_decision_sources",
        "cleanup_runs", "cleanup_items",
        "classification_runs", "classification_run_items",
        "classification_decisions", "classification_evidence",
    } <= tables


def test_lifecycle_models_preserve_text_ids_and_separate_summary_kinds(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        logical = LogicalItem(logical_key="li-synthetic", title="Synthetic")
        session.add(logical)
        session.flush()
        pending = ItemOccurrence(
            logical_item_id=logical.id, channel="pending", occurrence_key="pending:1",
            workitem_id_text="922337203685477580812345", process_id_text="process-1",
        )
        done = ItemOccurrence(
            logical_item_id=logical.id, channel="done", occurrence_key="done:9",
            workitem_id_text="-922337203685477580812346", process_id_text="process-1",
        )
        session.add_all([pending, done])
        session.flush()
        snapshot = ItemSnapshot(
            logical_item_id=logical.id, occurrence_id=pending.id, snapshot_kind="pending_initial",
            version=1, content_hash="a" * 64, payload_json="{}",
        )
        session.add(snapshot)
        session.flush()
        summaries = [
            SummaryVersion(logical_item_id=logical.id, snapshot_id=snapshot.id, summary_kind=kind,
                           version=1, status="candidate", input_hash=("b" if kind == "pending_assist" else "c") * 64,
                           structured_json="{}", provider_name="fake", model_name="synthetic", prompt_version="v1")
            for kind in ("pending_assist", "done_official")
        ]
        session.add_all(summaries)
        session.commit()

        assert pending.workitem_id_text != done.workitem_id_text
        assert {row.summary_kind for row in session.query(SummaryVersion)} == {"pending_assist", "done_official"}


def test_content_object_preserves_multiple_source_references(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        items = [
            OAItem(oa_item_key="source-a", source_channel="done", title="Synthetic A"),
            OAItem(oa_item_key="source-b", source_channel="done", title="Synthetic B"),
        ]
        session.add_all(items)
        session.flush()
        content = ContentObject(sha256="a" * 64, size_bytes=9, detected_type="pdf")
        session.add(content)
        session.flush()
        files = [
            ArchivedFile(oa_item_id=item.id, attachment_key=f"a-{index}", original_name="same.pdf", file_role="direct_attachment", source_container_key="root", depth=1, content_object_id=content.id)
            for index, item in enumerate(items)
        ]
        session.add_all(files)
        session.flush()
        document = KnowledgeDocument(knowledge_key="kd-synthetic", content_object_id=content.id, title="Synthetic")
        session.add(document)
        session.flush()
        session.add_all([
            SourceReference(knowledge_document_id=document.id, source_file_id=file.id, oa_item_id=file.oa_item_id)
            for file in files
        ])
        session.commit()

        assert session.query(ContentObject).count() == 1
        assert session.query(SourceReference).count() == 2


def test_archive_members_reuse_content_and_preserve_source_paths(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        logical = LogicalItem(logical_key="logical-synthetic", title="Synthetic")
        session.add(logical)
        session.flush()
        occurrence = ItemOccurrence(
            logical_item_id=logical.id, occurrence_key="pending:synthetic", channel="pending",
        )
        session.add(occurrence)
        session.flush()
        snapshot = ItemSnapshot(
            logical_item_id=logical.id, occurrence_id=occurrence.id, snapshot_kind="pending_initial",
            version=1, content_hash="1" * 64, payload_json="{}",
        )
        session.add(snapshot)
        session.flush()
        sources = [
            SourceAttachment(snapshot_id=snapshot.id, source_key=f"source-{index}", ordinal=index,
                             role="attachment", original_name=f"package-{index}.zip")
            for index in (1, 2)
        ]
        session.add_all(sources)
        session.flush()
        packages = [
            ArchivePackage(source_attachment_id=source.id, package_key=f"package-{index}",
                           original_name=source.original_name, sha256=str(index) * 64,
                           archive_format="zip", status="inspected", security_status="passed")
            for index, source in enumerate(sources, 1)
        ]
        session.add_all(packages)
        content = ContentObject(sha256="a" * 64, size_bytes=9, detected_type="pdf")
        session.add(content)
        session.flush()
        members = [
            ArchiveMember(archive_package_id=package.id, member_key=f"member-{index}",
                          original_path=f"folder-{index}/same.pdf", normalized_path=f"folder-{index}/same.pdf",
                          member_type="file", size_bytes=9, sha256=content.sha256,
                          content_object_id=content.id, status="verified")
            for index, package in enumerate(packages, 1)
        ]
        session.add_all(members)
        document = KnowledgeDocument(knowledge_key="knowledge-synthetic", content_object_id=content.id, title="Synthetic")
        session.add(document)
        session.flush()
        session.add(OAItemDocumentRelation(
            logical_item_id=logical.id, knowledge_document_id=document.id,
            source_attachment_id=sources[0].id, ordinal=1, role="supporting_attachment",
            is_main_document=False, display_title="Synthetic",
        ))
        session.commit()

        assert session.query(ContentObject).count() == 1
        assert {row.original_path for row in session.query(ArchiveMember)} == {
            "folder-1/same.pdf", "folder-2/same.pdf",
        }
        assert session.query(OAItemDocumentRelation).count() == 1


def test_migration_removes_only_empty_known_temp_table(tmp_path: Path) -> None:
    db = tmp_path / "state" / "oa.db"
    upgrade_database(db)
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE _alembic_tmp_batch_items (id INTEGER)")
    upgrade_database(db)
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='_alembic_tmp_batch_items'"
        ).fetchone() is None

    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE _alembic_tmp_batch_items (id INTEGER)")
        connection.execute("INSERT INTO _alembic_tmp_batch_items VALUES (1)")
    with pytest.raises(RuntimeError, match="manual recovery"):
        upgrade_database(db)


def test_text_identifier_and_unique_constraint(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    value = "922337203685477580812345"
    with Session(engine) as session:
        session.add(OAItem(oa_item_key="one", workitem_id_text=value, source_channel="done", title="synthetic"))
        session.commit()
        assert session.scalar(select(OAItem.workitem_id_text)) == value
        session.add(OAItem(oa_item_key="one", source_channel="done", title="duplicate"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_transaction_rolls_back(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with pytest.raises(RuntimeError):
        with session_scope(engine) as session:
            session.add(OAItem(oa_item_key="rollback", source_channel="done", title="synthetic"))
            raise RuntimeError("stop")
    with Session(engine) as session:
        assert session.scalar(select(OAItem).where(OAItem.oa_item_key == "rollback")) is None


def test_batch_item_uniqueness(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        batch = CollectionBatch(batch_key="b1", plan_hash="a" * 64, source_channel="done", planned_limit=20)
        session.add(batch)
        session.flush()
        session.add_all([
            BatchItem(batch_id=batch.id, oa_item_key="i1", workitem_id_text="-1", ordinal=1),
            BatchItem(batch_id=batch.id, oa_item_key="i1", workitem_id_text="-1", ordinal=2),
        ])
        with pytest.raises(IntegrityError):
            session.commit()
