import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
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
    Base,
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
    RebuildClassificationEvent,
    SourceReference,
    SourceAttachment,
    SummaryEvidence,
    SummaryJob,
    SummaryVersion,
)


def _migration_config(database_path: Path) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src" / "oa_knowledge" / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    return config


@pytest.fixture
def existing_0035_database(tmp_path: Path) -> Path:
    database_path = tmp_path / "oa.db"
    config = _migration_config(database_path)
    command.upgrade(config, "0035_markdown_item_indexes")
    with sqlite3.connect(database_path) as connection:
        # Migration 0001 creates the current SQLAlchemy metadata, so remove
        # fields that did not exist at revision 0035 before exercising 0036.
        connection.execute("DROP TABLE oa_items")
        connection.execute("""
            CREATE TABLE oa_items (
                id INTEGER NOT NULL,
                oa_item_key VARCHAR NOT NULL,
                logical_item_id INTEGER,
                workitem_id_text VARCHAR,
                source_channel VARCHAR NOT NULL,
                process_id_text VARCHAR,
                title TEXT NOT NULL,
                sender TEXT,
                department TEXT,
                document_number VARCHAR,
                initiated_at DATETIME,
                received_at DATETIME,
                completed_at DATETIME,
                oa_status VARCHAR,
                pipeline_status VARCHAR NOT NULL,
                archive_relpath TEXT,
                content_sha256 VARCHAR(64),
                source_type VARCHAR(20),
                internal_category VARCHAR(80),
                external_issuer TEXT,
                classification_version VARCHAR(20),
                first_seen_at DATETIME NOT NULL,
                last_seen_at DATETIME NOT NULL,
                PRIMARY KEY (id),
                UNIQUE (oa_item_key),
                FOREIGN KEY(logical_item_id) REFERENCES logical_items (id) ON DELETE SET NULL
            )
        """)
        connection.execute("CREATE INDEX ix_oa_items_logical_item_id ON oa_items (logical_item_id)")
        connection.execute("DROP TABLE rebuild_classification_events")
        connection.execute(
            "INSERT INTO oa_items (oa_item_key, source_channel, title, pipeline_status, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("synthetic-0035-item", "done", "Synthetic item", "discovered", "2026-08-21T00:00:00+00:00", "2026-08-21T00:00:00+00:00"),
        )
        item_id = connection.execute(
            "SELECT id FROM oa_items WHERE oa_item_key = 'synthetic-0035-item'"
        ).fetchone()[0]
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO files (
                id, oa_item_id, original_name, attachment_key, file_role,
                source_container_key, depth, download_status, download_attempts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (3501, item_id, "synthetic-cascade.pdf", "synthetic-cascade", "direct_attachment",
             "synthetic-container", 1, "verified", 0),
        )
        connection.execute(
            """
            INSERT INTO markdown_exports (
                id, source_file_id, oa_item_id, document_kind, source_sha256,
                source_relpath, markdown_relpath, parse_engine, parse_engine_version,
                parse_config_hash, schema_version, status, attempts, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (3502, 3501, item_id, "attachment", "a" * 64,
             "archive/synthetic-cascade.pdf", "markdown/synthetic-cascade.pdf.md",
             "synthetic", "1", "b" * 64, "synthetic-v1", "success", 0,
             "2026-08-21T00:00:00+00:00"),
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    return database_path


def upgrade(database_path: Path) -> None:
    command.upgrade(_migration_config(database_path), "head")


def table_columns(database_path: Path, table_name: str) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")}


def fetch_item(database_path: Path) -> sqlite3.Row:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute("SELECT * FROM oa_items WHERE oa_item_key = 'synthetic-0035-item'").fetchone()


def test_0036_adds_rebuild_classification_gate(existing_0035_database: Path) -> None:
    with sqlite3.connect(existing_0035_database) as connection:
        historical_columns = connection.execute("PRAGMA table_info(oa_items)").fetchall()
        historical_indexes = connection.execute("PRAGMA index_list(oa_items)").fetchall()
        historical_foreign_keys = connection.execute("PRAGMA foreign_key_list(oa_items)").fetchall()
        unique_index_columns = {
            tuple(row[2] for row in connection.execute(f"PRAGMA index_info({index[1]})"))
            for index in historical_indexes if index[2]
        }
    assert {row[1] for row in historical_columns if row[5]} == {"id"}
    assert {"oa_item_key", "source_type", "classification_version"} <= {row[1] for row in historical_columns}
    assert ("oa_item_key",) in unique_index_columns
    assert "ix_oa_items_logical_item_id" in {index[1] for index in historical_indexes}
    assert any(
        foreign_key[2] == "logical_items" and foreign_key[3] == "logical_item_id" and foreign_key[6] == "SET NULL"
        for foreign_key in historical_foreign_keys
    )
    with sqlite3.connect(existing_0035_database) as connection:
        assert connection.execute(
            "SELECT id, oa_item_id, original_name FROM files WHERE id = 3501"
        ).fetchone() == (3501, 1, "synthetic-cascade.pdf")
        assert connection.execute(
            "SELECT id, source_file_id, oa_item_id, markdown_relpath FROM markdown_exports WHERE id = 3502"
        ).fetchone() == (3502, 3501, 1, "markdown/synthetic-cascade.pdf.md")

    upgrade(existing_0035_database)
    columns = table_columns(existing_0035_database, "oa_items")
    assert {
        "document_date", "classification_state", "classification_confidence",
        "classification_confirmed_at", "classification_source",
    } <= columns
    row = fetch_item(existing_0035_database)
    assert row["classification_state"] == "needs_review"

    assert table_columns(existing_0035_database, "rebuild_classification_events") == {
        "id", "oa_item_id", "previous_classification_json", "current_classification_json", "actor", "created_at",
    }
    with sqlite3.connect(existing_0035_database) as connection:
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(oa_items)")}
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'oa_items'"
        ).fetchone()[0]
        cascade_child = connection.execute(
            "SELECT id, oa_item_id, original_name FROM files WHERE id = 3501"
        ).fetchone()
        set_null_child = connection.execute(
            "SELECT id, source_file_id, oa_item_id, markdown_relpath FROM markdown_exports WHERE id = 3502"
        ).fetchone()
        foreign_key_violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert "ix_oa_items_source_channel_classification_state_source_type" in indexes
    assert "ck_oa_items_classification_state" in table_sql
    assert "ck_oa_items_classification_source" in table_sql
    assert cascade_child == (3501, 1, "synthetic-cascade.pdf")
    assert set_null_child == (3502, 3501, 1, "markdown/synthetic-cascade.pdf.md")
    assert foreign_key_violations == []

    with sqlite3.connect(existing_0035_database) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE oa_items SET classification_state = 'invalid' WHERE oa_item_key = 'synthetic-0035-item'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE oa_items SET classification_source = 'invalid' WHERE oa_item_key = 'synthetic-0035-item'"
            )


def test_oa_item_classification_gate_and_event_audit_model(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        item = OAItem(oa_item_key="classification-synthetic", source_channel="done", title="Synthetic")
        session.add(item)
        session.flush()
        assert item.classification_state == "needs_review"
        assert item.document_date is None
        assert item.classification_confidence is None
        assert item.classification_confirmed_at is None
        assert item.classification_source is None

        event = RebuildClassificationEvent(
            oa_item_id=item.id,
            previous_classification_json='{"classification_state":"suggested"}',
            current_classification_json='{"classification_state":"confirmed"}',
            actor="local_web",
        )
        session.add(event)
        session.commit()

        persisted_event = session.scalar(select(RebuildClassificationEvent))
        assert persisted_event is not None
        assert persisted_event.oa_item_id == item.id
        assert persisted_event.current_classification_json == '{"classification_state":"confirmed"}'


def test_oa_item_classification_source_accepts_only_rule_or_manual(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        session.add_all([
            OAItem(oa_item_key="classification-rule", source_channel="done", title="Synthetic", classification_source="rule"),
            OAItem(oa_item_key="classification-manual", source_channel="done", title="Synthetic", classification_source="manual"),
        ])
        session.commit()

        session.add(OAItem(
            oa_item_key="classification-invalid-source",
            source_channel="done",
            title="Synthetic",
            classification_source="generated",
        ))
        with pytest.raises(IntegrityError):
            session.commit()


def test_orm_metadata_restricts_classification_source_to_rule_or_manual(tmp_path: Path) -> None:
    db = tmp_path / "orm-metadata.db"
    engine = create_db_engine(db)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            OAItem(oa_item_key="orm-rule", source_channel="done", title="Synthetic", classification_source="rule"),
            OAItem(oa_item_key="orm-manual", source_channel="done", title="Synthetic", classification_source="manual"),
        ])
        session.commit()

        session.add(OAItem(
            oa_item_key="orm-invalid-source",
            source_channel="done",
            title="Synthetic",
            classification_source="generated",
        ))
        with pytest.raises(IntegrityError):
            session.commit()


def test_orm_metadata_restricts_classification_state_to_gate_values(tmp_path: Path) -> None:
    db = tmp_path / "orm-classification-state.db"
    engine = create_db_engine(db)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(OAItem(
            oa_item_key="orm-invalid-state", source_channel="done", title="Synthetic",
            classification_state="invalid",
        ))
        with pytest.raises(IntegrityError):
            session.commit()


def test_migration_is_idempotent_and_wal_enabled(tmp_path: Path) -> None:
    db = tmp_path / "state" / "oa.db"
    upgrade_database(db)
    upgrade_database(db)
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0036_rebuild_classification_gate"
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
        "rebuild_classification_events",
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
