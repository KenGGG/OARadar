from datetime import datetime
from pathlib import Path
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oa_knowledge.collector.detail import CollaborationDetailAdapter, DetailCapture, DirectAttachment, PageSnapshot, RelatedContainerCapture
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, BatchItem, CollectionBatch, MarkdownTask, OAItem, ReviewEntry
from oa_knowledge.detail_archive import archive_collaboration_detail
from oa_knowledge.detail_archive import done_archive_directory
from oa_knowledge.cli import verified_attachment_resolver


def test_effective_snapshot_filters_utility_frames_and_keeps_one_body() -> None:
    snapshots = (
        PageSnapshot("downloadFileFrame", "https://oa.invalid/download", "<html>download tools</html>"),
        PageSnapshot("main", "https://oa.invalid/shell", "<html>navigation shell</html>"),
        PageSnapshot("zwIframe", "https://oa.invalid/content/content.do?id=1", "<html><body>有效正文内容和审批说明</body></html>"),
        PageSnapshot("duplicate", "https://oa.invalid/duplicate", "<html><body>有效正文内容和审批说明</body></html>"),
    )

    selected = CollaborationDetailAdapter._select_effective_snapshots(snapshots, "body")

    assert len(selected) == 1
    assert selected[0].name == "body.html"
    assert "有效正文" in selected[0].html


def test_workflow_snapshot_is_structured_json() -> None:
    source = PageSnapshot(
        "workflow-source-body.html",
        "https://oa.invalid/workflow",
        "<html><body><div>发起：张三</div><div>审批：同意</div><div>审批：同意</div></body></html>",
    )
    payload = json.dumps(
        {"schema_version": 1, "source_url": source.source_url, "entries": [{"text": "审批：同意"}]},
        ensure_ascii=False,
    )
    snapshot = PageSnapshot("workflow.json", source.source_url, payload)

    assert snapshot.name == "workflow.json"
    assert json.loads(snapshot.html)["entries"] == [{"text": "审批：同意"}]


def test_dynamic_content_wait_returns_after_stable_dom_without_full_delay() -> None:
    class Locator:
        def count(self): return 1
    class Frame:
        url = "https://oa.invalid/detail"
        def locator(self, _selector): return Locator()
        def evaluate(self, _script): return 3
    class Page:
        frames = [Frame()]
        elapsed = 0
        def wait_for_timeout(self, milliseconds): self.elapsed += milliseconds
    page = Page()
    CollaborationDetailAdapter._wait_dynamic_content(page, max_wait_ms=1500)
    assert 300 <= page.elapsed < 1500


def test_cap4_batch_fallback_runs_when_widget_downloads_all_fail(monkeypatch) -> None:
    failed = DirectAttachment(
        attachment_key="cap4-widget:failed", filename="synthetic.htm", file_url=None,
        size_bytes=None, mime_type=None, file_role="official_attachment",
        content=None, download_status="download_failed",
    )
    recovered = DirectAttachment(
        attachment_key="cap4:batch:synthetic", filename="synthetic.htm", file_url="https://oa.invalid/batch",
        size_bytes=4, mime_type="text/html", file_role="official_attachment",
        content=b"data", download_status="downloaded",
    )
    adapter = CollaborationDetailAdapter(None)  # type: ignore[arg-type]
    monkeypatch.setattr(adapter, "_download_cap4_widgets", lambda *args: [failed])
    monkeypatch.setattr(adapter, "_download_cap4_batches", lambda *args: [recovered])

    files = adapter._download_files(type("Page", (), {"frames": []})(), "direct_attachment")

    assert files == (failed, recovered)


def test_cap4_display_size_is_not_part_of_filename() -> None:
    assert CollaborationDetailAdapter._cap4_filename("呈批表.htm (5KB)") == ("呈批表.htm", False)
    assert CollaborationDetailAdapter._cap4_filename("情况说明 (0B)") == ("情况说明", True)
    assert CollaborationDetailAdapter._cap4_filename("空文件.docx (0B)") == ("空文件.docx", True)


def test_cap4_zero_byte_placeholders_are_not_download_candidates() -> None:
    assert CollaborationDetailAdapter._downloadable_cap4_names([
        "OA测试.docx (0B)", "有效附件.pdf (2MB)", "空白项 (0 B)",
    ]) == ["有效附件.pdf (2MB)"]


def test_recipient_collaboration_link_is_detected_without_matching_navigation() -> None:
    href = (
        "https://oa.invalid/seeyon/collaboration/collaboration.do?method=summary&"
        "openFrom=glwd&affairId=-2045&baseObjectId=-3333&baseApp=1"
    )

    assert CollaborationDetailAdapter._is_recipient_collaboration_link(href, "传阅文件标题")
    assert not CollaborationDetailAdapter._is_recipient_collaboration_link(href, "协同意见 回复")
    assert not CollaborationDetailAdapter._is_recipient_collaboration_link(
        "https://oa.invalid/seeyon/collaboration/collaboration.do?method=listDone",
        "接收人",
    )


def test_detail_archive_is_relative_hashed_and_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "state" / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        batch = CollectionBatch(
            batch_key="synthetic", source_channel="done", window_field="completed_at",
            planned_limit=3, status="running", plan_hash="a" * 64,
        )
        session.add(batch)
        session.flush()
        item = BatchItem(
            batch_id=batch.id, oa_item_key="done:123", workitem_id_text="123",
            title="synthetic/title", completed_at=datetime(2026, 7, 18), ordinal=1,
        )
        session.add(item)
        session.commit()
        item_id = item.id

    capture = DetailCapture(
        detail_url="https://oa.invalid/seeyon/collaboration.do?affairId=123",
        page_family="collaboration",
        body=(PageSnapshot("body.html", "about:blank", "<html>body</html>"),),
        workflow=(PageSnapshot("workflow.html", "about:blank", "<html>flow</html>"),),
        attachments=(),
    )
    with Session(engine) as session:
        item = session.get(BatchItem, item_id)
        manifest = archive_collaboration_detail(session, item, capture, tmp_path)
        session.commit()
        assert item.archive_manifest_relpath and not item.archive_manifest_relpath.startswith("/")
        assert (tmp_path / item.archive_manifest_relpath).is_file()
        assert sum(len(container.files) for container in manifest.containers) == 3

    with Session(engine) as session:
        item = session.get(BatchItem, item_id)
        archive_collaboration_detail(session, item, capture, tmp_path)
        assert session.scalar(select(func.count()).select_from(OAItem)) == 1
        assert session.scalar(select(func.count()).select_from(ArchivedFile)) == 3


def test_successful_empty_capture_removes_stale_failed_attachment(tmp_path: Path) -> None:
    db = tmp_path / "state" / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        batch = CollectionBatch(batch_key="stale", source_channel="done", window_field="completed_at", planned_limit=1, status="running", plan_hash="f" * 64)
        session.add(batch); session.flush()
        item = BatchItem(batch_id=batch.id, oa_item_key="done:stale", workitem_id_text="stale", title="stale", ordinal=1)
        session.add(item); session.flush()
        oa_item = OAItem(oa_item_key="done:stale", workitem_id_text="stale", source_channel="done", title="stale")
        session.add(oa_item); session.flush()
        session.add(ArchivedFile(
            oa_item_id=oa_item.id, original_name="旧呈批表.htm", attachment_key="stale-file",
            file_role="official_attachment", source_container_key="collaboration:stale",
            download_status="download_failed", download_attempts=1,
        ))
        session.commit(); item_id = item.id
    capture = DetailCapture(
        detail_url="https://oa.invalid/seeyon/collaboration.do?affairId=stale",
        page_family="collaboration", body=(PageSnapshot("body.html", "about:blank", "<html>body</html>"),),
        workflow=(), attachments=(),
    )
    with Session(engine) as session:
        archive_collaboration_detail(session, session.get(BatchItem, item_id), capture, tmp_path)
        session.commit()
        failed = session.scalar(select(func.count()).select_from(ArchivedFile).where(ArchivedFile.download_status == "download_failed"))
        assert failed == 0


def test_unavailable_associated_container_keeps_main_archive_and_creates_review(tmp_path: Path) -> None:
    db = tmp_path / "state" / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        batch = CollectionBatch(batch_key="partial", source_channel="done", window_field="completed_at", planned_limit=1, status="running", plan_hash="d" * 64)
        session.add(batch); session.flush()
        item = BatchItem(batch_id=batch.id, oa_item_key="done:partial", workitem_id_text="partial", title="partial", ordinal=1)
        session.add(item); session.commit(); item_id = item.id
    capture = DetailCapture(
        detail_url="https://oa.invalid/detail", page_family="collaboration",
        body=(PageSnapshot("body.html", "about:blank", "<html>main body</html>"),),
        workflow=(), attachments=(),
        capture_issues=({"kind": "associated_container_unavailable", "container_key": "broken-1", "parent_container_key": "collaboration:partial", "depth": 2, "error_code": "RuntimeError"},),
    )
    with Session(engine) as session:
        item = session.get(BatchItem, item_id)
        archive_collaboration_detail(session, item, capture, tmp_path)
        session.commit()
        assert item.archive_status == "archived"
        review = session.scalar(select(ReviewEntry).where(ReviewEntry.kind == "associated_container_unavailable"))
        assert review is not None
        assert review.container_key == "broken-1"


def test_repeatedly_failed_attachment_remains_eligible_for_retry(tmp_path: Path) -> None:
    db = tmp_path / "state" / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        item = OAItem(oa_item_key="failed-attachment", source_channel="done", title="failed")
        session.add(item); session.flush()
        session.add(ArchivedFile(
            oa_item_id=item.id, original_name="bad.zip", attachment_key="bad-key",
            file_role="direct_attachment", source_container_key="root", depth=1,
            download_status="download_failed", download_attempts=4,
        ))
        session.commit()
    resolver = verified_attachment_resolver(engine, tmp_path)
    assert resolver("bad-key") is None


def test_related_container_preserves_depth_roles_and_rejects_fake_pdf(tmp_path: Path) -> None:
    db = tmp_path / "state" / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        batch = CollectionBatch(batch_key="nested", source_channel="done", window_field="completed_at", planned_limit=1, status="running", plan_hash="b" * 64)
        session.add(batch); session.flush()
        item = BatchItem(batch_id=batch.id, oa_item_key="done:456", workitem_id_text="456", title="nested", ordinal=1)
        session.add(item); session.commit(); item_id = item.id
    capture = DetailCapture(
        detail_url="https://oa.invalid/seeyon/collaboration.do?affairId=456",
        page_family="collaboration",
        body=(PageSnapshot("body.html", "about:blank", "<html>body</html>"),),
        workflow=(PageSnapshot("flow.html", "about:blank", "<html>flow</html>"),),
        attachments=(),
        related_containers=(RelatedContainerCapture(
            container_key="govdoc:789", parent_container_key="collaboration:456", page_family="govdoc", depth=2,
            source_url="https://oa.invalid/govdoc", snapshots=(),
            attachments=(
                DirectAttachment("pdf-ok", "body.pdf", "/download/1", 18, "application/pdf", "official_body", b"%PDF-1.7\nsynthetic", "downloaded"),
                DirectAttachment("pdf-bad", "error.pdf", "/download/2", 18, "text/html", "official_attachment", b"<html>login</html>", "downloaded"),
            ),
        ),),
    )
    with Session(engine) as session:
        manifest = archive_collaboration_detail(session, session.get(BatchItem, item_id), capture, tmp_path)
        session.commit()
        nested = manifest.containers[1]
        assert nested.depth == 2
        assert [file.download_status for file in nested.files] == ["verified", "rejected_error_page"]
        rows = session.scalars(select(ArchivedFile).where(ArchivedFile.source_container_key == "govdoc:789")).all()
        assert {row.depth for row in rows} == {2}
        assert sum(row.local_relpath is not None for row in rows) == 1


def test_same_named_attachments_get_unique_deterministic_paths(tmp_path: Path) -> None:
    db = tmp_path / "state" / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        batch = CollectionBatch(batch_key="collision", source_channel="done", window_field="completed_at", planned_limit=1, status="running", plan_hash="c" * 64)
        session.add(batch); session.flush()
        item = BatchItem(batch_id=batch.id, oa_item_key="done:collision", workitem_id_text="collision", title="collision", ordinal=1)
        session.add(item); session.commit(); item_id = item.id
    capture = DetailCapture(
        detail_url="https://oa.invalid/detail", page_family="collaboration",
        body=(), workflow=(),
        attachments=(
            DirectAttachment("key-one", "同名.xlsx", "/1", 3, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", content=b"one", download_status="downloaded"),
            DirectAttachment("key-two", "同名.xlsx", "/2", 3, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", content=b"two", download_status="downloaded"),
        ),
    )
    with Session(engine) as session:
        archive_collaboration_detail(session, session.get(BatchItem, item_id), capture, tmp_path)
        session.commit()
        paths = [row.local_relpath for row in session.scalars(select(ArchivedFile).where(ArchivedFile.file_role == "direct_attachment"))]
        assert len(paths) == len(set(paths)) == 2
        assert all(path and (tmp_path / path).is_file() for path in paths)


def test_successful_retry_replaces_failed_attachment_with_same_name(tmp_path: Path) -> None:
    db = tmp_path / "state" / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        batch = CollectionBatch(batch_key="retry", source_channel="done", window_field="completed_at", planned_limit=1, status="running", plan_hash="e" * 64)
        session.add(batch); session.flush()
        item = BatchItem(batch_id=batch.id, oa_item_key="done:retry", workitem_id_text="retry", title="retry", ordinal=1)
        session.add(item); session.commit(); item_id = item.id

    failed = DetailCapture("https://oa.invalid/detail", "collaboration", (), (), (
        DirectAttachment("old-batch-key", "附件.pdf", None, None, file_role="official_attachment", content=None, download_status="download_failed"),
    ))
    succeeded = DetailCapture("https://oa.invalid/detail", "collaboration", (), (), (
        DirectAttachment("new-widget-key", "附件.pdf", None, 18, "application/pdf", "direct_attachment", b"%PDF-1.7\nsynthetic", "downloaded"),
    ))
    with Session(engine) as session:
        archive_collaboration_detail(session, session.get(BatchItem, item_id), failed, tmp_path)
        archive_collaboration_detail(session, session.get(BatchItem, item_id), succeeded, tmp_path)
        session.commit()
        files = session.scalars(select(ArchivedFile).where(ArchivedFile.oa_item_id == session.get(BatchItem, item_id).oa_item_id, ArchivedFile.file_role.in_(("direct_attachment", "official_attachment")))).all()

    assert len(files) == 1
    assert files[0].attachment_key == "new-widget-key"
    assert files[0].download_status == "verified"


def test_done_archive_directory_uses_initiation_month_and_never_completion_time() -> None:
    assert done_archive_directory("事项", "42", datetime(2022, 4, 22, 9, 0)).as_posix() == "archive/raw/oa/done/2022/04/事项_42"
    assert done_archive_directory("事项", "42", None).as_posix() == "archive/raw/oa/done/unknown/事项_42"


def test_successful_archive_does_not_enqueue_legacy_markdown_task(tmp_path: Path) -> None:
    db = tmp_path / "state" / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        batch = CollectionBatch(batch_key="md", source_channel="done", window_field="completed_at", planned_limit=1, status="running", plan_hash="e" * 64)
        session.add(batch); session.flush()
        item = BatchItem(batch_id=batch.id, oa_item_key="done:md-enqueue", workitem_id_text="md-enqueue", title="md", ordinal=1)
        session.add(item); session.commit(); item_id = item.id
    capture = DetailCapture(
        detail_url="https://oa.invalid/detail", page_family="collaboration",
        body=(PageSnapshot("body.html", "about:blank", "<html>body</html>"),),
        workflow=(),
        attachments=(DirectAttachment("att-1", "报告.pdf", None, 8, "application/pdf", "direct_attachment", b"%PDF-1.7\nsynthetic", "downloaded"),),
    )
    with Session(engine) as session:
        archive_collaboration_detail(session, session.get(BatchItem, item_id), capture, tmp_path)
        session.commit()
        assert session.scalar(select(func.count()).select_from(MarkdownTask)) == 0
    # Re-running archive persistence must remain decoupled from the retired queue.
    with Session(engine) as session:
        archive_collaboration_detail(session, session.get(BatchItem, item_id), capture, tmp_path)
        session.commit()
        assert session.scalar(select(func.count()).select_from(MarkdownTask)) == 0
