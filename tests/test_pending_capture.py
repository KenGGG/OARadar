from pathlib import Path

from sqlalchemy.orm import Session

from oa_knowledge.collector.detail import DetailCapture, DirectAttachment, PageSnapshot, RelatedContainerCapture
from oa_knowledge.collector.pending import DiscoveredPendingItem
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, ContentObject, ItemSnapshot, OAItem, SourceAttachment
from oa_knowledge.pending_archive import persist_pending_capture
from oa_knowledge.pending_sync import sync_pending_discovery


def test_pending_capture_creates_real_snapshot_sources_and_content_reuse(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    item = DiscoveredPendingItem(
        affair_id_text="affair-synthetic", title="Synthetic", sender="Synthetic Sender",
        previous_approver=None, initiated_at=None, received_at=None, deadline_text=None,
        reminder_count=0, processing_status="待处理", current_node="经办", importance=None, ordinal=1,
    )
    capture = DetailCapture(
        detail_url="https://oa.synthetic.invalid/detail", page_family="collaboration",
        body=(PageSnapshot("body.html", "https://oa.synthetic.invalid/body", "<p>Body</p>"),),
        workflow=(PageSnapshot("workflow.json", "https://oa.synthetic.invalid/flow", '{"entries":[]}'),),
        attachments=(DirectAttachment(
            attachment_key="attachment-1", filename="evidence.txt", file_url=None,
            size_bytes=7, mime_type="text/plain", content=b"content", download_status="verified",
        ),),
    )
    with Session(engine) as session:
        sync_pending_discovery(session, [item])
        first = persist_pending_capture(session, "pending:affair-synthetic", capture, tmp_path)
        second = persist_pending_capture(session, "pending:affair-synthetic", capture, tmp_path)
        session.commit()

        assert first.snapshot_id == second.snapshot_id
        assert session.query(ItemSnapshot).count() == 1
        assert session.query(OAItem).filter_by(source_channel="pending").count() == 1
        assert session.query(SourceAttachment).count() == 1
        assert session.query(ContentObject).count() == 1
        assert session.query(ArchivedFile).filter_by(download_status="verified").count() == 3
        source = session.query(SourceAttachment).one()
        assert source.content_object_id is not None
        assert source.download_status == "verified"
        assert (tmp_path / session.query(ArchivedFile).filter_by(file_role="direct_attachment").one().local_relpath).is_file()


def test_pending_capture_persists_recipient_related_container_attachments(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    item = DiscoveredPendingItem(
        affair_id_text="affair-related", title="Synthetic related", sender="Synthetic Sender",
        previous_approver=None, initiated_at=None, received_at=None, deadline_text=None,
        reminder_count=0, processing_status="待处理", current_node="经办", importance=None, ordinal=1,
    )
    capture = DetailCapture(
        detail_url="https://oa.synthetic.invalid/detail", page_family="collaboration",
        body=(PageSnapshot("body.html", "https://oa.synthetic.invalid/body", "<p>Body</p>"),),
        workflow=(), attachments=(),
        related_containers=(RelatedContainerCapture(
            container_key="recipient:summary-1", parent_container_key="collaboration:affair-related",
            page_family="collaboration", depth=2,
            source_url="https://oa.synthetic.invalid/recipient",
            snapshots=(PageSnapshot("recipient.html", "https://oa.synthetic.invalid/recipient", "<p>Recipient</p>"),),
            attachments=(DirectAttachment(
                attachment_key="recipient-file-1", filename="circulated.pdf", file_url=None,
                size_bytes=8, mime_type="application/pdf", file_role="official_attachment",
                content=b"%PDF-1.4\nsynthetic", download_status="verified",
            ),),
        ),),
    )
    with Session(engine) as session:
        sync_pending_discovery(session, [item])
        result = persist_pending_capture(session, "pending:affair-related", capture, tmp_path)
        session.commit()

        assert result.source_attachment_count == 1
        source = session.query(SourceAttachment).one()
        assert source.role == "official_attachment"
        archived = session.get(ArchivedFile, source.source_file_id)
        assert archived is not None
        assert archived.source_container_key == "recipient:summary-1"
        assert archived.depth == 2
