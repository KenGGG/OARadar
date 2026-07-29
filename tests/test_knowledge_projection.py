from pathlib import Path

from sqlalchemy.orm import Session

from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import (
    ArchivedFile, ContentObject, ItemOccurrence, ItemSnapshot, KnowledgeDocument,
    LogicalItem, OAItem, OAItemDocumentRelation, ParseArtifact, ParseJob, SourceAttachment, SourceReference,
)
from oa_knowledge.knowledge_projection import publish_pending_projection


def test_pending_projection_writes_linked_draft_documents_and_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"; upgrade_database(db); engine = create_db_engine(db)
    parsed = tmp_path / "parse/1/artifact-1/document.md"
    parsed.parent.mkdir(parents=True); parsed.write_text("# Parsed\n\nReal parsed text", encoding="utf-8")
    with Session(engine) as session:
        logical = LogicalItem(logical_key="logical-synthetic", title="Synthetic", lifecycle_status="identity_pending")
        session.add(logical); session.flush()
        occurrence = ItemOccurrence(logical_item_id=logical.id, occurrence_key="pending:1", channel="pending", title="Synthetic")
        session.add(occurrence); session.flush()
        snapshot = ItemSnapshot(logical_item_id=logical.id, occurrence_id=occurrence.id, snapshot_kind="pending_initial", version=1, content_hash="a"*64, payload_json="{}")
        session.add(snapshot)
        item = OAItem(oa_item_key="pending:1", logical_item_id=logical.id, source_channel="pending", title="Synthetic")
        session.add(item); session.flush()
        content = ContentObject(sha256="b"*64, size_bytes=10, detected_type="pdf")
        session.add(content); session.flush()
        file = ArchivedFile(oa_item_id=item.id, attachment_key="a", original_name="attachment.pdf", file_role="direct_attachment", source_container_key="pending:1", depth=1, local_relpath="raw/a.pdf", download_status="verified", sha256=content.sha256, content_object_id=content.id)
        session.add(file); session.flush()
        source = SourceAttachment(snapshot_id=snapshot.id, source_file_id=file.id, source_key="a", ordinal=1, role="direct_attachment", original_name="attachment.pdf", download_status="verified", content_object_id=content.id)
        session.add(source)
        job = ParseJob(file_id=file.id, engine="markitdown", engine_version="1", config_hash="c", status="completed")
        session.add(job); session.flush()
        artifact = ParseArtifact(parse_job_id=job.id, content_object_id=content.id, engine="markitdown", engine_version="1", output_relpath="1/artifact-1/document.md", source_sha256=content.sha256, config_hash="c", page_map_json="{}", quality_score=1.0, quality_status="ok", lifecycle_status="valid")
        session.add(artifact); session.flush(); content.active_parse_artifact_id = artifact.id

        first = publish_pending_projection(session, logical.id, tmp_path)
        second = publish_pending_projection(session, logical.id, tmp_path)
        session.commit()

        assert first == second
        assert (tmp_path / first.oa_overview_relpath).is_file()
        assert len(first.knowledge_relpaths) == 1
        knowledge_path = tmp_path / first.knowledge_relpaths[0]
        text = knowledge_path.read_text(encoding="utf-8")
        assert "publication_status: draft_pending" in text
        assert "Real parsed text" in text
        assert session.query(KnowledgeDocument).count() == 1
        assert session.query(SourceReference).count() == 1
        assert session.query(OAItemDocumentRelation).count() == 1
