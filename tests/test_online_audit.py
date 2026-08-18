from datetime import datetime, timezone
import json
from pathlib import Path

from sqlalchemy.orm import Session

from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import OAManifestItem, OnlineAuditItem, OnlineAuditRun, OperationJob, PipelineTask
from oa_knowledge.online_audit import (
    AuditObservation, audit_view, canonical_downloaded_count,
    classify_attachment_counts, classify_evidence, execute_audit,
    evidence_is_historical_subset, explain_evidence_difference, fingerprint_attachments, pause_audit, restart_audit, resume_audit,
    requeue_changed_item_for_latest_audit, requeue_supplemented_item,
    start_audit, unique_capture_attachment_count,
)


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


def test_start_replaces_orphaned_active_run_whose_job_is_terminal(config_file: Path) -> None:
    settings, engine = setup(config_file)
    first = start_audit(settings)
    with Session(engine) as session:
        run = session.get(OnlineAuditRun, first["run_id"])
        operation = session.get(OperationJob, run.job_id)
        operation.status = "failed"
        session.commit()

    replacement = start_audit(settings)

    assert replacement["run_id"] != first["run_id"]
    with Session(engine) as session:
        assert session.get(OnlineAuditRun, first["run_id"]).status == "failed"


def test_execute_records_counts_timing_errors_and_implementation_log(config_file: Path) -> None:
    settings, engine = setup(config_file)
    run_id = start_audit(settings)["run_id"]

    def inspect(item: OnlineAuditItem) -> AuditObservation:
        if item.oa_item_key == "done:2":
            raise RuntimeError("authorization: Bearer forbidden token=secret")
        return AuditObservation(recognized_attachments=3)

    execute_audit(settings, run_id, inspect_item=inspect)
    first_pass = audit_view(settings, run_id)
    assert first_pass["run"]["status"] == "queued"
    assert first_pass["run"]["access_failed_items"] == 0
    assert any(event["event_type"] == "access_failures_requeued" for event in first_pass["events"])

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


def test_execute_rechecks_completed_items_that_lack_per_attachment_evidence(config_file: Path) -> None:
    settings, engine = setup(config_file)
    run_id = start_audit(settings)["run_id"]
    with Session(engine) as session:
        item = session.query(OnlineAuditItem).filter_by(run_id=run_id).first()
        item.status = "matched"
        item.comparison_reason = None
        session.commit()

    inspected: list[int] = []
    execute_audit(
        settings, run_id,
        inspect_item=lambda item: (
            inspected.append(item.id)
            or AuditObservation(recognized_attachments=0, attachment_evidence=())
        ),
    )

    assert len(inspected) == 2
    with Session(engine) as session:
        rows = session.query(OnlineAuditItem).filter_by(run_id=run_id).all()
        assert all(item.comparison_reason == "exact_match" for item in rows)


def test_execute_yields_after_bounded_batch_and_remains_resumable(config_file: Path) -> None:
    settings, engine = setup(config_file)
    run_id = start_audit(settings)["run_id"]

    execute_audit(
        settings, run_id,
        inspect_item=lambda _item: AuditObservation(recognized_attachments=0, attachment_evidence=()),
        max_items=1,
    )

    with Session(engine) as session:
        run = session.get(OnlineAuditRun, run_id)
        job = session.get(OperationJob, run.job_id)
        assert run.status == "queued"
        assert run.completed_items == 1
        assert job.status == "queued"
        assert job.lease_owner is None

    execute_audit(
        settings, run_id,
        inspect_item=lambda _item: AuditObservation(recognized_attachments=0, attachment_evidence=()),
        max_items=1,
    )
    assert audit_view(settings, run_id)["run"]["status"] == "completed"


def test_execute_yields_after_wall_clock_budget(config_file: Path, monkeypatch) -> None:
    settings, engine = setup(config_file)
    run_id = start_audit(settings)["run_id"]
    ticks = iter((0.0, 0.0, 1.0, 61.0))
    monkeypatch.setattr("oa_knowledge.online_audit.monotonic", lambda: next(ticks))

    execute_audit(
        settings, run_id,
        inspect_item=lambda _item: AuditObservation(recognized_attachments=0, attachment_evidence=()),
        max_seconds=60,
    )

    with Session(engine) as session:
        run = session.get(OnlineAuditRun, run_id)
        assert run.status == "queued"
        assert run.completed_items == 1


def test_execute_refreshes_long_running_job_lease_and_clears_stale_error(config_file: Path) -> None:
    settings, engine = setup(config_file)
    run_id = start_audit(settings)["run_id"]
    with Session(engine) as session:
        run = session.get(OnlineAuditRun, run_id)
        job = session.get(OperationJob, run.job_id)
        job.status = "running"
        job.lease_owner = "worker-synthetic"
        job.lease_expires_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
        job.last_error_code = "recovered_expired_lease"
        session.commit()

    observed: dict[str, object] = {}

    def inspect(_item: OnlineAuditItem) -> AuditObservation:
        with Session(engine) as session:
            run = session.get(OnlineAuditRun, run_id)
            job = session.get(OperationJob, run.job_id)
            observed.update(
                heartbeat_at=job.heartbeat_at,
                lease_expires_at=job.lease_expires_at,
                last_error_code=job.last_error_code,
            )
        return AuditObservation(recognized_attachments=0)

    execute_audit(settings, run_id, inspect_item=inspect)

    assert observed["heartbeat_at"] is not None
    assert observed["lease_expires_at"] is not None
    assert observed["lease_expires_at"].year > 2000
    assert observed["last_error_code"] is None
    with Session(engine) as session:
        run = session.get(OnlineAuditRun, run_id)
        job = session.get(OperationJob, run.job_id)
        assert job.status == "completed"
        assert job.last_error_code is None


def test_missing_online_download_is_enqueued_for_safe_realtime_capture(config_file: Path) -> None:
    settings, engine = setup(config_file)
    run_id = start_audit(settings)["run_id"]

    execute_audit(
        settings, run_id,
        inspect_item=lambda _item: AuditObservation(recognized_attachments=1),
    )

    with Session(engine) as session:
        tasks = session.query(PipelineTask).filter_by(
            queue_name="realtime_done", stage="done_capture_and_archive",
        ).all()
        assert len(tasks) == 2
        assert all(task.idempotency_key.startswith(f"online-audit:{run_id}:") for task in tasks)
        assert all(task.status == "queued" for task in tasks)


def test_successful_supplement_reopens_item_and_completed_audit(config_file: Path) -> None:
    settings, engine = setup(config_file)
    run_id = start_audit(settings)["run_id"]
    execute_audit(
        settings, run_id,
        inspect_item=lambda _item: AuditObservation(recognized_attachments=1),
    )
    with Session(engine) as session:
        item = session.query(OnlineAuditItem).filter_by(run_id=run_id).first()
        assert item.status == "missing_download"
        assert requeue_supplemented_item(session, run_id, item.oa_item_key) is True
        session.commit()

    with Session(engine) as session:
        run = session.get(OnlineAuditRun, run_id)
        item = session.query(OnlineAuditItem).filter_by(
            run_id=run_id, oa_item_key="done:1",
        ).one()
        job = session.get(OperationJob, run.job_id)
        assert item.status == "pending"
        assert item.finished_at is None
        assert run.status == "queued"
        assert run.completed_items == 1
        assert job.status == "queued"


def test_audit_enrolls_manifest_item_added_during_final_batch(config_file: Path) -> None:
    settings, engine = setup(config_file)
    run_id = start_audit(settings)["run_id"]
    added = False

    def inspect(_item: OnlineAuditItem) -> AuditObservation:
        nonlocal added
        if not added:
            with Session(engine) as other:
                other.add(OAManifestItem(
                    oa_item_key="done:late", workitem_id_text="late",
                    title="后加入的合成事项", list_page=2,
                    processing_status="pending_download",
                ))
                other.commit()
            added = True
        return AuditObservation(recognized_attachments=0)

    execute_audit(settings, run_id, inspect_item=inspect)

    with Session(engine) as session:
        run = session.get(OnlineAuditRun, run_id)
        assert run.status == "queued"
        assert run.total_items == 3
        assert run.completed_items == 2
    execute_audit(
        settings, run_id,
        inspect_item=lambda _item: AuditObservation(recognized_attachments=0),
    )
    with Session(engine) as session:
        run = session.get(OnlineAuditRun, run_id)
        assert run.status == "completed"
        assert run.completed_items == 3
        late = session.query(OnlineAuditItem).filter_by(
            run_id=run_id, oa_item_key="done:late",
        ).one()
        assert late.status == "matched"


def test_scheduled_done_recapture_invalidates_completed_audit_evidence(config_file: Path) -> None:
    settings, engine = setup(config_file)
    run_id = start_audit(settings)["run_id"]
    execute_audit(
        settings, run_id,
        inspect_item=lambda _item: AuditObservation(recognized_attachments=0),
    )
    with Session(engine) as session:
        assert requeue_changed_item_for_latest_audit(session, "done:1") is True
        session.commit()

    with Session(engine) as session:
        run = session.get(OnlineAuditRun, run_id)
        item = session.query(OnlineAuditItem).filter_by(
            run_id=run_id, oa_item_key="done:1",
        ).one()
        assert run.status == "queued"
        assert run.completed_items == 1
        assert item.status == "pending"
        assert item.comparison_reason is None
        assert item.online_evidence_json == "[]"


def test_attachment_classification_does_not_treat_markdown_lag_as_count_mismatch() -> None:
    assert classify_attachment_counts(2, 2, 2, 0) == "matched"
    assert classify_attachment_counts(3, 2, 2, 2) == "missing_download"
    assert classify_attachment_counts(1, 2, 2, 2) == "historical_retained"
    assert classify_attachment_counts(2, 4, 2, 2) == "matched"


def test_fingerprint_and_classification_detect_same_count_content_change() -> None:
    online_inventory, online_content = fingerprint_attachments([
        ("official_attachment", "key-1", 10, "a" * 64),
        ("official_body", "key-2", 20, "b" * 64),
    ])
    local_inventory, local_content = fingerprint_attachments([
        ("official_body", "key-2", 20, "b" * 64),
        ("official_attachment", "key-1", 10, "c" * 64),
    ])

    assert online_inventory == local_inventory
    assert online_content != local_content
    assert classify_evidence(
        recognized=2, downloaded=2,
        online_inventory=online_inventory, local_inventory=local_inventory,
        online_content=online_content, local_content=local_content,
        depth_limit_reached=False,
    ) == "content_mismatch"


def test_inventory_change_is_not_hidden_by_extra_local_attachment_count() -> None:
    assert classify_evidence(
        recognized=1, downloaded=2,
        online_inventory="online", local_inventory="local",
        online_content="online-content", local_content="local-content",
        depth_limit_reached=False,
    ) == "inventory_mismatch"


def test_evidence_explanation_distinguishes_identity_drift_from_content_change() -> None:
    original = [("official_attachment", "old-key", 10, "a" * 64)]
    assert explain_evidence_difference(
        [("official_attachment", "new-key", 10, "a" * 64)], original,
    ) == "attachment_identity_changed"
    assert explain_evidence_difference(
        [("official_attachment", "old-key", 10, "b" * 64)], original,
    ) == "content_changed"
    assert explain_evidence_difference(
        [("associated_document", "old-key", 10, "a" * 64)], original,
    ) == "attachment_role_changed"
    assert explain_evidence_difference(
        [("associated_document", "new-key", 10, "a" * 64)], original,
    ) == "attachment_metadata_changed"
    assert explain_evidence_difference(original, original) == "exact_match"


def test_current_online_evidence_subset_is_retained_history_not_mismatch() -> None:
    current = [("official_attachment", "key-1", 10, "a" * 64)]
    local = current + [("official_body", "old-body", 20, "b" * 64)]

    assert evidence_is_historical_subset(current, local) is True
    assert explain_evidence_difference(current, local) == "historical_retained"
    assert evidence_is_historical_subset(local, current) is False


def test_resume_repairs_previously_misclassified_retained_history(config_file: Path) -> None:
    settings, engine = setup(config_file)
    run_id = start_audit(settings)["run_id"]
    current = (("official_attachment", "key-1", 10, "a" * 64),)
    local = current + (("official_body", "old-body", 20, "b" * 64),)
    with Session(engine) as session:
        item = session.query(OnlineAuditItem).filter_by(run_id=run_id).first()
        item.status = "inventory_mismatch"
        item.comparison_reason = "inventory_changed"
        item.online_evidence_json = json.dumps([
            {"role": role, "key": key, "size": size, "sha256": digest}
            for role, key, size, digest in current
        ])
        item.local_evidence_json = json.dumps([
            {"role": role, "key": key, "size": size, "sha256": digest}
            for role, key, size, digest in local
        ])
        session.commit()

    execute_audit(
        settings, run_id,
        inspect_item=lambda _item: AuditObservation(recognized_attachments=0, attachment_evidence=()),
        max_items=1,
    )

    with Session(engine) as session:
        item = session.query(OnlineAuditItem).filter_by(run_id=run_id).first()
        assert item.status == "historical_retained"
        assert item.comparison_reason == "historical_retained"


def test_resume_repairs_stale_summary_hashes_from_exact_attachment_evidence(config_file: Path) -> None:
    settings, engine = setup(config_file)
    run_id = start_audit(settings)["run_id"]
    evidence = (("official_attachment", "key-1", 10, "a" * 64),)
    encoded = json.dumps([
        {"role": role, "key": key, "size": size, "sha256": digest}
        for role, key, size, digest in evidence
    ])
    inventory, content = fingerprint_attachments(list(evidence))
    with Session(engine) as session:
        item = session.query(OnlineAuditItem).filter_by(run_id=run_id).first()
        item.status = "content_mismatch"
        item.comparison_reason = "exact_match"
        item.recognized_attachments = 1
        item.downloaded_attachments = 1
        item.online_evidence_json = encoded
        item.local_evidence_json = encoded
        item.online_inventory_sha256 = inventory
        item.local_inventory_sha256 = inventory
        item.online_content_sha256 = content
        item.local_content_sha256 = None
        session.commit()

    execute_audit(
        settings, run_id,
        inspect_item=lambda _item: AuditObservation(recognized_attachments=0, attachment_evidence=()),
        max_items=1,
    )

    with Session(engine) as session:
        item = session.query(OnlineAuditItem).filter_by(run_id=run_id).first()
        assert item.status == "matched"
        assert item.local_content_sha256 == content
        assert item.comparison_reason == "exact_match"


def test_execute_persists_per_attachment_evidence_without_file_bytes(config_file: Path) -> None:
    settings, engine = setup(config_file)
    run_id = start_audit(settings)["run_id"]
    evidence = (("official_attachment", "synthetic-key", 10, "a" * 64),)

    execute_audit(
        settings, run_id,
        inspect_item=lambda _item: AuditObservation(
            recognized_attachments=1,
            online_inventory_sha256=fingerprint_attachments(list(evidence))[0],
            online_content_sha256=fingerprint_attachments(list(evidence))[1],
            attachment_evidence=evidence,
        ),
    )

    with Session(engine) as session:
        item = session.query(OnlineAuditItem).filter_by(run_id=run_id).first()
        persisted = json.loads(item.online_evidence_json)
        assert persisted == [{
            "role": "official_attachment", "key": "synthetic-key",
            "size": 10, "sha256": "a" * 64,
        }]
        assert "content" not in item.online_evidence_json
        assert item.comparison_reason == "inventory_changed"
    assert audit_view(settings, run_id)["comparison_reasons"] == {"inventory_changed": 2}


def test_evidence_classification_requires_complete_online_content_and_honors_depth_limit() -> None:
    assert classify_evidence(
        recognized=1, downloaded=1, online_inventory="i", local_inventory="i",
        online_content=None, local_content="c", depth_limit_reached=False,
    ) == "content_unverified"
    assert classify_evidence(
        recognized=1, downloaded=1, online_inventory="i", local_inventory="i",
        online_content="c", local_content="c", depth_limit_reached=True,
    ) == "depth_limit_reached"


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
