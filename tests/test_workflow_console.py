"""Synthetic regressions for the three-flow console's recovery/read contracts."""
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import Base, ArchivedFile, ItemOccurrence, LogicalItem, MarkdownExport, OAItem, OAManifestItem, PipelineTask
from oa_knowledge.web import console_views


@pytest.fixture
def local_db(config_file):
    settings = load_settings(config_file)
    engine = create_db_engine(settings.database_path)
    Base.metadata.create_all(engine)
    yield settings, engine
    engine.dispose()


def seed_done(session):
    item = OAItem(oa_item_key="done:synthetic", source_channel="done", title="Synthetic report", archive_relpath="originals/done/synthetic")
    manifest = OAManifestItem(oa_item_key=item.oa_item_key, title=item.title, list_page=1, processing_status="downloaded")
    session.add_all([item, manifest]); session.flush()
    file = ArchivedFile(oa_item_id=item.id, attachment_key="synthetic-a", original_name="synthetic.txt", file_role="direct_attachment", source_container_key="synthetic", download_status="verified", local_relpath="originals/done/synthetic/synthetic.txt")
    session.add(file); session.flush()
    export = MarkdownExport(oa_item_id=item.id, source_file_id=file.id, source_sha256="a" * 64, source_relpath=file.local_relpath, markdown_relpath="raw/sources/oa/synthetic.txt.md", parse_engine="synthetic", parse_engine_version="1", parse_config_hash="1", schema_version="1", status="failed")
    session.add(export); session.flush()
    return item, manifest, file, export


def test_rebuild_persists_one_local_delivery_task_and_reuses_it(local_db):
    settings, engine = local_db
    with Session(engine) as session:
        item, _, _, export = seed_done(session)
        item_key, export_id = item.oa_item_key, export.id
        session.commit()
    first = console_views.rebuild_markdown_export(settings, export_id)
    second = console_views.rebuild_markdown_export(settings, export_id)
    with Session(engine) as session:
        tasks = list(session.scalars(select(PipelineTask).where(PipelineTask.logical_item_key == item_key)))
        assert len(tasks) == 1
        assert tasks[0].queue_name == "markdown_delivery"
        assert tasks[0].stage == "attachment_inventory"
        assert first["task_id"] == second["task_id"] == tasks[0].id


def test_pending_server_filters_before_pagination(local_db):
    settings, engine = local_db
    with Session(engine) as session:
        logical = LogicalItem(logical_key="filter", title="synthetic")
        session.add(logical); session.flush()
        for n in range(4):
            session.add(ItemOccurrence(logical_item_id=logical.id, channel="pending", occurrence_key=f"p:{n}", title=f"synthetic {n}", occurrence_status="active", cleanup_status="cleanup_failed" if n % 2 else "not_eligible"))
        session.commit()
    result = console_views.pending_notifications_list(settings, filter_kind="cleanup_failed", page=2, page_size=1, query="synthetic")
    assert result["total"] == 2
    assert len(result["items"]) == 1
    assert result["items"][0]["cleanup_status"] == "cleanup_failed"


def test_summary_retry_rejects_cleaned_occurrence(local_db):
    settings, engine = local_db
    with Session(engine) as session:
        logical = LogicalItem(logical_key="synthetic", title="", lifecycle_status="pending")
        session.add(logical); session.flush()
        occurrence = ItemOccurrence(logical_item_id=logical.id, channel="pending", occurrence_key="p:clean", occurrence_status="cleaned", cleanup_status="cleaned")
        session.add(occurrence); session.commit(); oid = occurrence.id
    with pytest.raises(ValueError):
        console_views.retry_pending_summary(settings, oid)
    with Session(engine) as session:
        assert session.scalar(select(PipelineTask)) is None


def test_summary_retry_rejects_an_unfailed_summary(local_db):
    settings, engine = local_db
    with Session(engine) as session:
        logical = LogicalItem(logical_key="active", title="synthetic")
        session.add(logical); session.flush()
        occurrence = ItemOccurrence(logical_item_id=logical.id, channel="pending", occurrence_key="p:active", occurrence_status="active")
        session.add(occurrence); session.commit(); oid = occurrence.id
    with pytest.raises(ValueError):
        console_views.retry_pending_summary(settings, oid)


def test_cleaned_pending_api_never_returns_retained_business_payload(local_db, monkeypatch):
    settings, engine = local_db
    with Session(engine) as session:
        logical = LogicalItem(logical_key="retained", title="synthetic private")
        session.add(logical); session.flush()
        occurrence = ItemOccurrence(logical_item_id=logical.id, channel="pending", occurrence_key="p:retained", occurrence_status="cleaned", cleanup_status="cleaned")
        session.add(occurrence); session.commit(); oid = occurrence.id
    monkeypatch.setattr(console_views, "lifecycle_pending_detail", lambda *args: {"title": "synthetic private", "sender": "synthetic private", "current_node": "synthetic private", "snapshot": {"payload": "synthetic private"}, "ollama_summary": {"summary": "synthetic private"}, "attachments": [{"name": "synthetic private"}], "evidence_files": [{"name": "synthetic private"}]})
    detail = console_views.pending_notification_detail(settings, oid)
    assert "synthetic private" not in str(detail)
    assert detail["can_retry_summary"] is False
    assert detail["can_cleanup"] is False


def test_concurrent_summary_retries_share_one_task(local_db):
    from concurrent.futures import ThreadPoolExecutor
    from oa_knowledge.db.models import ItemSnapshot, SummaryJob
    settings, engine = local_db
    with Session(engine) as session:
        logical = LogicalItem(logical_key="concurrent", title="synthetic")
        session.add(logical); session.flush()
        occurrence = ItemOccurrence(logical_item_id=logical.id, channel="pending", occurrence_key="p:concurrent", occurrence_status="active")
        session.add(occurrence); session.flush()
        snapshot = ItemSnapshot(logical_item_id=logical.id, occurrence_id=occurrence.id, snapshot_kind="pending", version=1, content_hash="0" * 64, payload_json="{}")
        session.add(snapshot); session.flush()
        session.add(SummaryJob(logical_item_id=logical.id, snapshot_id=snapshot.id, summary_kind="pending", status="failed", idempotency_key="synthetic-summary"))
        session.commit(); oid = occurrence.id
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: console_views.retry_pending_summary(settings, oid), range(2)))
    assert results[0]["task_id"] == results[1]["task_id"]
    with Session(engine) as session:
        tasks = list(session.scalars(select(PipelineTask)))
        assert len(tasks) == 1
        assert tasks[0].logical_item_id is not None


def test_markdown_retry_deduplicates_beyond_display_history(local_db):
    from oa_knowledge.web.workflow_views import retry_markdown_item
    settings, engine = local_db
    with Session(engine) as session:
        item, _, _, _ = seed_done(session)
        for n in range(25):
            session.add(PipelineTask(queue_name="markdown_delivery", priority=50, logical_item_key=item.oa_item_key, stage="attachment_inventory", status="queued" if n == 0 else "completed", idempotency_key=f"synthetic:{n}"))
        session.commit(); iid = item.id
        first = session.scalar(select(PipelineTask.id).order_by(PipelineTask.id))
    assert retry_markdown_item(settings, iid)["task_id"] == first


def test_api_save_and_preview_contract(local_db, config_file):
    from fastapi.testclient import TestClient
    from oa_knowledge.web import create_web_app
    settings, engine = local_db
    with Session(engine) as session:
        item, _, _, export = seed_done(session)
        export.status = "success"
        session.commit(); eid, iid = export.id, item.id
    path = settings.workspace_root / "raw/sources/oa/synthetic.txt.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<script>synthetic only</script>", encoding="utf-8")
    with TestClient(create_web_app(settings, config_path=config_file)) as client:
        assert client.get(f"/api/markdown-outputs/items/{iid}").status_code == 200
        content = client.get(f"/api/markdown-outputs/documents/{eid}/content")
        assert content.json() == {"text": "<script>synthetic only</script>", "truncated": False}
        saved = client.patch("/api/settings", json={"markdown_export": {"enabled": False}}, headers={"x-csrf-token": client.cookies["oa_csrf"]})
        assert saved.status_code == 200
        displayed = client.get("/api/settings").json()
        assert displayed["markdown"]["enabled"] is False
        assert displayed["restart_required"] is True


def test_unknown_delivery_reconciliation_requires_confirmation_and_matching_record(local_db):
    from oa_knowledge.db.models import NotificationDelivery
    settings, engine = local_db
    with Session(engine) as session:
        logical = LogicalItem(logical_key="reconcile", title="synthetic")
        session.add(logical); session.flush()
        occurrence = ItemOccurrence(logical_item_id=logical.id, channel="pending", occurrence_key="p:reconcile", occurrence_status="active")
        delivery = NotificationDelivery(logical_item_id=logical.id, channel="feishu", notification_type="pending_summary", idempotency_key="synthetic-delivery", status="unknown")
        session.add_all([occurrence, delivery]); session.commit(); oid, did = occurrence.id, delivery.id
    with pytest.raises(ValueError):
        console_views.reconcile_pending_delivery(settings, oid, delivery_id=did, outcome="sent", confirmed=False)
    with pytest.raises(ValueError):
        console_views.reconcile_pending_delivery(settings, oid, delivery_id=did + 1, outcome="sent", confirmed=True)
    result = console_views.reconcile_pending_delivery(settings, oid, delivery_id=did, outcome="sent", confirmed=True)
    assert result["status"] == "sent"
    with Session(engine) as session:
        assert session.get(NotificationDelivery, did).sent_at is not None
        assert session.scalar(select(PipelineTask)) is None  # no automatic send or cleanup


def test_markdown_search_and_pagination_use_item_total(local_db):
    settings, engine = local_db
    with Session(engine) as session:
        for n in range(105):
            session.add(OAItem(oa_item_key=f"done:{n}", source_channel="done", title=f"synthetic {n:03}"))
        session.commit()
    result = console_views.markdown_outputs_list(settings, page=3, page_size=50, query="synthetic")
    assert result["item_total"] == 105
    assert len(result["items"]) == 5
    assert console_views.markdown_outputs_list(settings, query="synthetic 104")["item_total"] == 1


def test_markdown_preview_rejects_traversal_and_symlinks(local_db, tmp_path):
    from oa_knowledge.web.workflow_views import markdown_document_path
    settings, engine = local_db
    with Session(engine) as session:
        _, _, _, export = seed_done(session)
        export.status = "success"
        session.commit(); eid = export.id
    path = settings.workspace_root / "raw/sources/oa/synthetic.txt.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Synthetic", encoding="utf-8")
    assert markdown_document_path(settings, eid) == path
    path.unlink()
    outside = tmp_path / "private.txt"
    outside.write_text("synthetic secret", encoding="utf-8")
    path.symlink_to(outside)
    with pytest.raises(ValueError):
        markdown_document_path(settings, eid)


def test_rebuild_does_not_bypass_depth_limit_or_review_gates(local_db):
    from oa_knowledge.web.workflow_views import retry_markdown_item
    settings, engine = local_db
    with Session(engine) as session:
        item, manifest, _, _ = seed_done(session)
        manifest.processing_status = "depth_limit_reached"
        session.commit(); item_id = item.id
    with pytest.raises(ValueError):
        retry_markdown_item(settings, item_id)
    with Session(engine) as session:
        assert session.scalar(select(PipelineTask)) is None


def test_archive_retry_rejects_complete_and_deduplicates_active_task(local_db):
    settings, engine = local_db
    with Session(engine) as session:
        _, manifest, _, _ = seed_done(session)
        session.commit(); mid = manifest.id
    with pytest.raises(ValueError):
        console_views.retry_done_archive(settings, mid)
    with Session(engine) as session:
        manifest = session.get(OAManifestItem, mid)
        manifest.processing_status = "partial"
        session.commit()
    first = console_views.retry_done_archive(settings, mid)
    second = console_views.retry_done_archive(settings, mid)
    assert first["task_id"] == second["task_id"]
    with Session(engine) as session:
        tasks = list(session.scalars(select(PipelineTask)))
        assert len(tasks) == 1 and tasks[0].stage == "done_capture_and_archive"
