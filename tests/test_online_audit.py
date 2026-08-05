from pathlib import Path

from sqlalchemy.orm import Session

from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import OAManifestItem, OnlineAuditItem, OnlineAuditRun
from oa_knowledge.online_audit import AuditObservation, audit_view, canonical_downloaded_count, classify_attachment_counts, execute_audit, pause_audit, restart_audit, resume_audit, start_audit, unique_capture_attachment_count


def setup(config_file: Path):
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        session.add_all([
            OAManifestItem(oa_item_key="done:1", workitem_id_text="1", title="Alpha", list_page=1),
            OAManifestItem(oa_item_key="done:2", workitem_id_text="2", title="Beta", list_page=1),
        ])
        session.commit()
    return settings, engine


def test_start_pause_and_resume_are_durable(config_file: Path) -> None:
    settings, engine = setup(config_file)
    created = start_audit(settings)
    assert created["total_items"] == 2
    assert start_audit(settings)["run_id"] == created["run_id"]
    assert pause_audit(settings, created["run_id"])["status"] == "paused"
    assert resume_audit(settings, created["run_id"])["status"] == "queued"
    with Session(engine) as session:
        assert session.query(OnlineAuditItem).count() == 2


def test_execute_records_counts_timing_errors_and_implementation_log(config_file: Path) -> None:
    settings, engine = setup(config_file)
    run_id = start_audit(settings)["run_id"]

    def inspect(item: OnlineAuditItem) -> AuditObservation:
        if item.oa_item_key == "done:2":
            raise RuntimeError("authorization: Bearer forbidden token=secret")
        return AuditObservation(recognized_attachments=3)

    execute_audit(settings, run_id, inspect_item=inspect)
    payload = audit_view(settings, run_id)
    assert payload["run"]["status"] == "completed"
    assert payload["run"]["completed_items"] == 2
    assert payload["run"]["access_failed_items"] == 1
    by_key = {row["oa_item_key"]: row for row in payload["items"]}
    assert by_key["done:1"]["recognized_attachments"] == 3
    assert by_key["done:1"]["elapsed_seconds"] is not None
    assert by_key["done:2"]["error_code"] == "OA_ACCESS_ERROR"
    assert "secret" not in by_key["done:2"]["error_detail"]
    assert any(event["event_type"] == "item_failed" for event in payload["events"])
    with Session(engine) as session:
        assert session.get(OnlineAuditRun, run_id).finished_at is not None


def test_audit_view_paginates_items_with_total(config_file: Path) -> None:
    settings, _ = setup(config_file)
    run_id = start_audit(settings)["run_id"]
    first = audit_view(settings, run_id, item_page=1, item_page_size=1)
    second = audit_view(settings, run_id, item_page=2, item_page_size=1)
    assert first["item_pagination"] == {"page": 1, "page_size": 1, "total": 2, "pages": 2}
    assert len(first["items"]) == len(second["items"]) == 1
    assert first["items"][0]["id"] != second["items"][0]["id"]


def test_execute_recovers_item_left_running_by_interruption(config_file: Path) -> None:
    settings, engine = setup(config_file)
    run_id = start_audit(settings)["run_id"]
    with Session(engine) as session:
        item = session.query(OnlineAuditItem).filter_by(run_id=run_id).order_by(OnlineAuditItem.id).first()
        item.status = "running"
        session.commit()
    execute_audit(settings, run_id, inspect_item=lambda _item: AuditObservation(recognized_attachments=0))
    assert audit_view(settings, run_id)["run"]["completed_items"] == 2


def test_attachment_classification_does_not_treat_markdown_lag_as_count_mismatch() -> None:
    assert classify_attachment_counts(2, 2, 2, 0) == "matched"
    assert classify_attachment_counts(3, 2, 2, 2) == "missing_download"
    assert classify_attachment_counts(1, 2, 2, 2) == "historical_retained"
    assert classify_attachment_counts(2, 4, 2, 2) == "matched"


def test_capture_count_deduplicates_same_attachment_across_containers() -> None:
    from types import SimpleNamespace
    attachment = SimpleNamespace(attachment_key="same-key", file_role="official_attachment")
    capture = SimpleNamespace(attachments=(attachment,), related_containers=(SimpleNamespace(attachments=(attachment,)),))
    assert unique_capture_attachment_count(capture) == 1


def test_canonical_downloaded_count_preserves_distinct_oa_links_with_same_content() -> None:
    assert canonical_downloaded_count(recognized=4, verified_rows=4, unique_hashes=3) == 4
    assert canonical_downloaded_count(recognized=4, verified_rows=3, unique_hashes=3) == 3
    assert canonical_downloaded_count(recognized=0, verified_rows=2, unique_hashes=2) == 2


def test_restart_supersedes_old_run_and_starts_from_zero(config_file: Path) -> None:
    settings, _ = setup(config_file)
    old_id = start_audit(settings)["run_id"]
    new = restart_audit(settings)
    assert new["run_id"] != old_id
    assert new["status"] == "queued"
    assert audit_view(settings, new["run_id"])["run"]["completed_items"] == 0
