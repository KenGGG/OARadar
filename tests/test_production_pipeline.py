import hashlib
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import (
    ArchivedFile, ContentObject, CuratedRun, ItemOccurrence, LogicalItem, OAItem,
    OAManifestItem, OnlineAuditItem, OnlineAuditRun, ParseArtifact, ParseJob, PipelineTask,
)
from oa_knowledge.production_pipeline import CORE_PIPELINE_STAGES, ProductionQueue
from oa_knowledge.web.lifecycle_views import processing_center


def _queue(config_file: Path) -> tuple[ProductionQueue, object]:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    return ProductionQueue(engine), settings


def _authorize_history(queue: ProductionQueue, *logical_keys: str) -> None:
    with Session(queue.engine) as session:
        session.add_all([
            OAItem(
                oa_item_key=logical_key,
                source_channel="done",
                title=f"synthetic-{index}",
                archive_relpath=f"originals/unknown/unknown_{index}",
            )
            for index, logical_key in enumerate(logical_keys, start=1)
        ])
        audit = OnlineAuditRun(
            status="completed",
            total_items=len(logical_keys),
            completed_items=len(logical_keys),
        )
        session.add(audit)
        session.flush()
        session.add_all([
            OnlineAuditItem(
                run_id=audit.id,
                oa_item_key=logical_key,
                title=f"synthetic-{index}",
                status="matched",
                comparison_reason="exact_match",
                depth_limit_reached=False,
            )
            for index, logical_key in enumerate(logical_keys, start=1)
        ])
        session.commit()


def test_realtime_pending_and_done_are_claimed_before_historical(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    _authorize_history(queue, "item-3")
    queue.enqueue("historical_done_backfill", "item-3", "parse", "history-3")
    queue.enqueue("realtime_done", "item-2", "detail_sync", "done-2")
    queue.enqueue("realtime_pending", "item-1", "detail_sync", "pending-1")

    first = queue.claim("worker-a")
    second = queue.claim("worker-a")
    third = queue.claim("worker-a")

    assert [first.queue_name, second.queue_name, third.queue_name] == [
        "realtime_pending", "realtime_done", "historical_done_backfill",
    ]


def test_retire_non_core_tasks_marks_existing_curation_task_nonrecoverable(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    task_id = queue.enqueue("historical_done_backfill", "done:legacy", "curation", "retire-curation")

    assert "curation" not in CORE_PIPELINE_STAGES
    assert queue.retire_non_core_tasks() == 1
    assert queue.claim("worker-a") is None

    with Session(queue.engine) as session:
        task = session.get(PipelineTask, task_id)
        assert task is not None
        assert task.status == "failed"
        assert task.error_code == "RETIRED_STAGE"
        assert task.recoverable is False


def test_historical_wave_finishes_parse_before_ollama(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    _authorize_history(queue, "item-1", "item-2")
    ollama_id = queue.enqueue("historical_done_backfill", "item-1", "ollama_extract", "history-1")
    parse_id = queue.enqueue("historical_done_backfill", "item-2", "parse", "history-2")

    first = queue.claim("worker-a")

    assert first.id == parse_id
    assert first.id != ollama_id


def test_historical_wave_orders_source_publish_before_curation(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    _authorize_history(queue, "item-1", "item-2")
    curation_id = queue.enqueue("historical_done_backfill", "item-1", "curation", "history-curation")
    publish_id = queue.enqueue("historical_done_backfill", "item-2", "source_publish", "history-publish")

    first = queue.claim("worker-a")

    assert first.id == publish_id
    assert first.id != curation_id


def test_historical_wave_does_not_block_realtime(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    queue.enqueue("historical_done_backfill", "item-1", "parse", "history-1")
    realtime_id = queue.enqueue("realtime_pending", "item-2", "detail_sync", "pending-2")

    assert queue.claim("worker-a").id == realtime_id


def test_completed_audit_allows_only_safe_canonical_history(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    with Session(queue.engine) as session:
        session.add_all((
            OAItem(
                oa_item_key="done:legacy", source_channel="done", title="legacy",
                archive_relpath="raw/done/unknown/legacy",
            ),
            OAItem(
                oa_item_key="done:canonical", source_channel="done", title="canonical",
                archive_relpath="originals/unknown/unknown_canonical",
            ),
            OAItem(
                oa_item_key="done:unsafe-canonical", source_channel="done", title="unsafe",
                archive_relpath="originals/unknown/unknown_unsafe",
            ),
        ))
        audit = OnlineAuditRun(status="completed", total_items=3, completed_items=3)
        session.add(audit); session.flush()
        session.add_all((
            OnlineAuditItem(
                run_id=audit.id, oa_item_key="done:legacy", title="legacy",
                status="matched", comparison_reason="exact_match",
            ),
            OnlineAuditItem(
                run_id=audit.id, oa_item_key="done:canonical", title="canonical",
                status="matched", comparison_reason="exact_match",
            ),
            OnlineAuditItem(
                run_id=audit.id, oa_item_key="done:unsafe-canonical", title="unsafe",
                status="content_mismatch", comparison_reason="content_changed",
            ),
        ))
        session.commit()
    legacy_id = queue.enqueue(
        "historical_done_backfill", "done:legacy", "parse", "history-gated-legacy",
    )
    canonical_id = queue.enqueue(
        "historical_done_backfill", "done:canonical", "parse", "history-gated-canonical",
    )
    unsafe_id = queue.enqueue(
        "historical_done_backfill", "done:unsafe-canonical", "parse", "history-gated-unsafe",
    )

    claimed = queue.claim("worker-a")

    assert claimed is not None and claimed.id == canonical_id
    queue.complete(claimed.id, "worker-a")
    assert queue.claim("worker-a") is None
    with Session(queue.engine) as session:
        assert session.get(PipelineTask, legacy_id).status == "queued"
        assert session.get(PipelineTask, unsafe_id).status == "queued"


def test_first_deploy_does_not_claim_unaudited_history(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    with Session(queue.engine) as session:
        session.add_all((
            OAItem(
                oa_item_key="done:legacy-first-deploy", source_channel="done", title="legacy",
                archive_relpath="raw/done/unknown/legacy",
            ),
            OAItem(
                oa_item_key="done:canonical-first-deploy", source_channel="done", title="canonical",
                archive_relpath="originals/unknown/unknown_canonical",
            ),
        ))
        session.commit()
    task_ids = [
        queue.enqueue(
            "historical_done_backfill", logical_key, "parse", f"unaudited:{logical_key}",
        )
        for logical_key in (
            "done:legacy-first-deploy",
            "done:canonical-first-deploy",
            "done:missing-first-deploy",
        )
    ]

    assert queue.claim("worker-a") is None
    with Session(queue.engine) as session:
        assert {
            session.get(PipelineTask, task_id).status for task_id in task_ids
        } == {"queued"}


def test_newer_running_audit_blocks_history_despite_older_safe_evidence(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    with Session(queue.engine) as session:
        session.add(OAItem(
            oa_item_key="done:stale-evidence", source_channel="done", title="stale",
            archive_relpath="originals/unknown/unknown_stale",
        ))
        completed = OnlineAuditRun(status="completed", total_items=1, completed_items=1)
        session.add(completed); session.flush()
        session.add(OnlineAuditItem(
            run_id=completed.id, oa_item_key="done:stale-evidence", title="stale",
            status="matched", comparison_reason="exact_match",
        ))
        session.add(OnlineAuditRun(status="running", total_items=1, completed_items=0))
        session.commit()
    task_id = queue.enqueue(
        "historical_done_backfill", "done:stale-evidence", "parse",
        "history:done:stale-evidence:knowledge-v2",
    )

    assert queue.claim("worker-a") is None
    with Session(queue.engine) as session:
        assert session.get(PipelineTask, task_id).status == "queued"


def test_finalize_ineligible_historical_tasks_parks_only_review_rows(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    with Session(queue.engine) as session:
        session.add_all((
            OAItem(
                oa_item_key="done:safe", source_channel="done", title="safe",
                archive_relpath="originals/unknown/unknown_safe",
            ),
            OAItem(
                oa_item_key="done:unsafe", source_channel="done", title="unsafe",
                archive_relpath="originals/unknown/unknown_unsafe",
            ),
            OAItem(
                oa_item_key="done:migration-failed", source_channel="done", title="migration-failed",
                archive_relpath="raw/done/unknown/migration-failed",
            ),
        ))
        audit = OnlineAuditRun(status="completed", total_items=3, completed_items=3)
        session.add(audit); session.flush()
        audit_id = audit.id
        session.add_all((
            OnlineAuditItem(
                run_id=audit.id, oa_item_key="done:safe", title="safe",
                status="matched", comparison_reason="exact_match",
            ),
            OnlineAuditItem(
                run_id=audit.id, oa_item_key="done:unsafe", title="unsafe",
                status="content_mismatch", comparison_reason="content_changed",
            ),
            OnlineAuditItem(
                run_id=audit.id, oa_item_key="done:migration-failed", title="migration-failed",
                status="matched", comparison_reason="exact_match",
            ),
        ))
        session.commit()
    safe_id = queue.enqueue(
        "historical_done_backfill", "done:safe", "parse", "history:done:safe:knowledge-v2",
    )
    unsafe_id = queue.enqueue(
        "historical_done_backfill", "done:unsafe", "parse", "history:done:unsafe:knowledge-v2",
    )
    migration_failed_id = queue.enqueue(
        "historical_done_backfill", "done:migration-failed", "parse",
        "history:done:migration-failed:knowledge-v2",
    )
    realtime_id = queue.enqueue(
        "realtime_done", "done:realtime", "parse", "realtime:done:realtime",
    )

    finalized = queue.finalize_ineligible_historical_tasks(audit_id)

    assert finalized == 2
    with Session(queue.engine) as session:
        assert session.get(PipelineTask, safe_id).status == "queued"
        for task_id in (unsafe_id, migration_failed_id):
            task = session.get(PipelineTask, task_id)
            assert task.status == "failed"
            assert task.recoverable is False
            assert task.error_code == "ONLINE_AUDIT_REVIEW_REQUIRED"
            assert task.finished_at is not None
        assert session.get(PipelineTask, realtime_id).status == "queued"


def test_new_safe_audit_releases_previously_review_gated_history(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    with Session(queue.engine) as session:
        session.add(OAItem(
            oa_item_key="done:resolved", source_channel="done", title="resolved",
            archive_relpath="originals/unknown/unknown_resolved",
        ))
        audit = OnlineAuditRun(status="completed", total_items=1, completed_items=1)
        session.add(audit); session.flush()
        audit_id = audit.id
        session.add(OnlineAuditItem(
            run_id=audit.id, oa_item_key="done:resolved", title="resolved",
            status="matched", comparison_reason="exact_match",
        ))
        session.commit()
    task_id = queue.enqueue(
        "historical_done_backfill", "done:resolved", "parse",
        "history:done:resolved:knowledge-v2",
    )
    with Session(queue.engine) as session:
        task = session.get(PipelineTask, task_id)
        task.status = "failed"
        task.recoverable = False
        task.error_code = "ONLINE_AUDIT_REVIEW_REQUIRED"
        task.last_error = "old review gate"
        task.finished_at = task.updated_at
        session.commit()

    released = queue.release_verified_historical_tasks(audit_id)

    assert released == 1
    with Session(queue.engine) as session:
        task = session.get(PipelineTask, task_id)
        assert task.status == "queued"
        assert task.stage == "attachment_inventory"
        assert task.recoverable is True
        assert task.error_code is None
        assert task.last_error is None
        assert task.finished_at is None


def test_realtime_task_that_advances_yields_to_an_unstarted_peer(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    first_id = queue.enqueue("realtime_pending", "item-1", "detail_sync", "pending-yield-1")
    second_id = queue.enqueue("realtime_pending", "item-2", "detail_sync", "pending-yield-2")

    first = queue.claim("worker-a")
    queue.advance(first.id, "worker-a", "pending_parse")
    second = queue.claim("worker-a")

    assert first.id == first_id
    assert second.id == second_id


def test_realtime_done_archives_before_running_local_curation(config_file: Path) -> None:
    """New OA bytes must be protected before an older task occupies the local model."""
    queue, _ = _queue(config_file)
    curation_id = queue.enqueue(
        "realtime_done", "item-curation", "curation", "done-curation",
    )
    capture_id = queue.enqueue(
        "realtime_done", "item-capture", "done_capture_and_archive", "done-capture",
    )

    claimed = queue.claim("worker-a")

    assert claimed.id == capture_id
    assert claimed.id != curation_id


def test_enqueue_is_idempotent_and_survives_new_queue_instance(config_file: Path) -> None:
    queue, settings = _queue(config_file)
    first = queue.enqueue("realtime_pending", "logical-7", "summarize", "same-key")
    second = ProductionQueue(create_db_engine(settings.database_path)).enqueue(
        "realtime_pending", "logical-7", "summarize", "same-key"
    )

    assert first == second
    with Session(queue.engine) as session:
        assert session.query(PipelineTask).count() == 1


def test_pausing_history_does_not_pause_realtime_claims(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    queue.set_historical_paused(True)
    queue.enqueue("historical_done_backfill", "item-3", "parse", "history-3")
    queue.enqueue("realtime_done", "item-2", "detail_sync", "done-2")

    claimed = queue.claim("worker-a")

    assert claimed.queue_name == "realtime_done"
    assert queue.claim("worker-a") is None


def test_processing_center_reports_real_queue_state(config_file: Path) -> None:
    queue, settings = _queue(config_file)
    queue.enqueue("realtime_pending", "logical-1", "ollama_summary", "pending-1")
    queue.enqueue("historical_done_backfill", "logical-2", "parse", "history-2")
    queue.set_historical_paused(True)

    result = processing_center(settings)

    assert result["queues"]["realtime_pending"]["queued"] == 1
    assert result["queues"]["historical_done_backfill"]["queued"] == 1
    assert result["historical_paused"] is True
    assert result["mock_data"] is False


def test_processing_center_reports_history_idle_without_active_work(config_file: Path) -> None:
    _, settings = _queue(config_file)

    result = processing_center(settings)

    assert result["historical_state"] == "idle"


def test_bootstrap_enqueues_pending_and_unparsed_done_without_duplicates(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    with Session(queue.engine) as session:
        logical = LogicalItem(logical_key="pending:1", title="新待办")
        session.add(logical); session.flush()
        session.add(ItemOccurrence(logical_item_id=logical.id, occurrence_key="p-1", channel="pending", occurrence_status="active"))
        session.add(OAManifestItem(oa_item_key="d-1", title="历史已办", list_page=1, processing_status="downloaded"))
        session.commit()

    first = queue.bootstrap_current_state()
    second = queue.bootstrap_current_state()

    assert first == {"realtime_pending": 1, "historical_done_backfill": 1}
    assert second == {"realtime_pending": 0, "historical_done_backfill": 0}


def test_start_historical_rebuild_requeues_completed_campaign_when_idle(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    with Session(queue.engine) as session:
        session.add(OAManifestItem(
            oa_item_key="d-rerun", title="需要重建的历史已办", list_page=1,
            processing_status="downloaded",
        ))
        session.commit()
    _authorize_history(queue, "d-rerun")

    first = queue.start_historical_rebuild()
    task = queue.claim("worker-a")
    assert first == {"created": 1, "requeued": 0, "repaired_legacy": 0, "already_active": 0}
    assert task is not None
    queue.complete(task.id, "worker-a")

    second = queue.start_historical_rebuild()

    assert second == {"created": 0, "requeued": 1, "repaired_legacy": 0, "already_active": 0}
    with Session(queue.engine) as session:
        rerun = session.get(PipelineTask, task.id)
        assert rerun.status == "queued"
        assert rerun.stage == "attachment_inventory"
        assert rerun.attempts == 0
        assert rerun.finished_at is None


def test_start_historical_rebuild_does_not_reset_campaign_while_active(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    with Session(queue.engine) as session:
        session.add(OAManifestItem(
            oa_item_key="d-active", title="正在重建的历史已办", list_page=1,
            processing_status="downloaded",
        ))
        session.commit()

    queue.start_historical_rebuild()

    assert queue.start_historical_rebuild() == {
        "created": 0, "requeued": 0, "repaired_legacy": 0, "already_active": 1,
    }


def test_start_historical_rebuild_keeps_review_gated_tasks_parked(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    task_id = queue.enqueue(
        "historical_done_backfill", "done:review", "attachment_inventory",
        "history:done:review:knowledge-v2",
    )
    with Session(queue.engine) as session:
        task = session.get(PipelineTask, task_id)
        task.status = "failed"
        task.recoverable = False
        task.error_code = "ONLINE_AUDIT_REVIEW_REQUIRED"
        session.commit()

    result = queue.start_historical_rebuild()

    assert result["requeued"] == 0
    with Session(queue.engine) as session:
        task = session.get(PipelineTask, task_id)
        assert task.status == "failed"
        assert task.recoverable is False
        assert task.error_code == "ONLINE_AUDIT_REVIEW_REQUIRED"


def test_bootstrap_resumes_fully_parsed_done_at_source_publish(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    with Session(queue.engine) as session:
        manifest = OAManifestItem(
            oa_item_key="done:parsed", title="已解析历史事项", list_page=1,
            processing_status="downloaded",
        )
        item = OAItem(
            oa_item_key="done:parsed", source_channel="done", title="已解析历史事项",
            archive_relpath="originals/unknown/unknown_synthetic",
        )
        session.add_all([manifest, item]); session.flush()
        content = ContentObject(sha256=hashlib.sha256(b"source").hexdigest(), size_bytes=6)
        session.add(content); session.flush()
        source = ArchivedFile(
            oa_item_id=item.id, attachment_key="source", file_role="direct_attachment",
            source_container_key="root", original_name="source.docx",
            local_relpath="originals/unknown/unknown_synthetic/source.docx",
            download_status="verified", sha256=content.sha256, content_object_id=content.id,
        )
        session.add(source); session.flush()
        job = ParseJob(
            file_id=source.id, engine="synthetic", engine_version="1",
            config_hash="c" * 64, status="completed",
        )
        session.add(job); session.flush()
        artifact = ParseArtifact(
            parse_job_id=job.id, content_object_id=content.id, engine="synthetic",
            engine_version="1", output_relpath="synthetic/document.md",
            source_sha256=content.sha256, product_sha256="d" * 64,
            config_hash="c" * 64, lifecycle_status="valid",
        )
        session.add(artifact); session.flush()
        content.active_parse_artifact_id = artifact.id
        session.commit()

    assert queue.bootstrap_current_state()["historical_done_backfill"] == 1
    with Session(queue.engine) as session:
        task = session.scalar(select(PipelineTask).where(
            PipelineTask.logical_item_key == "done:parsed",
        ))
        assert task.stage == "source_publish"
        assert task.idempotency_key.endswith(":knowledge-v2")


def test_finish_and_retry_are_durable_and_do_not_block_next_task(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    failed_id = queue.enqueue("realtime_pending", "one", "detail_sync", "one")
    next_id = queue.enqueue("realtime_done", "two", "attachment_inventory", "two")
    assert queue.claim("worker-a").id == failed_id

    queue.fail(failed_id, "worker-a", "OA_DETAIL_FETCH_FAILED", "sanitized", recoverable=False)
    assert queue.claim("worker-a").id == next_id
    queue.advance(next_id, "worker-a", "parse", progress_current=1, progress_total=3)

    with Session(queue.engine) as session:
        failed = session.get(PipelineTask, failed_id)
        advanced = session.get(PipelineTask, next_id)
        assert failed.status == "failed"
        assert failed.error_code == "OA_DETAIL_FETCH_FAILED"
        assert advanced.status == "queued"
        assert advanced.stage == "parse"
        assert advanced.attempts == 0
        assert (advanced.progress_current, advanced.progress_total) == (1, 3)


def test_explicit_retry_resets_failed_production_task(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    task_id = queue.enqueue("realtime_done", "done:retry", "parse", "retry-me")
    assert queue.claim("worker-a").id == task_id
    with Session(queue.engine) as session:
        task = session.get(PipelineTask, task_id)
        task.attempts = task.max_attempts
        session.commit()
    queue.fail(task_id, "worker-a", "PARSE_FAILED", "sanitized", recoverable=True)

    assert queue.retry_failed() == 1
    with Session(queue.engine) as session:
        task = session.get(PipelineTask, task_id)
        assert task.status == "queued"
        assert task.attempts == 0
        assert task.error_code is None


def test_explicit_retry_keeps_nonrecoverable_review_failure_parked(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    task_id = queue.enqueue("realtime_done", "done:review", "source_publish", "review-me")
    assert queue.claim("worker-a").id == task_id
    queue.fail(task_id, "worker-a", "UNSUPPORTED_SOURCE_FORMAT", "sanitized", recoverable=False)

    assert queue.retry_failed() == 0
    with Session(queue.engine) as session:
        task = session.get(PipelineTask, task_id)
        assert task.status == "failed"
        assert task.recoverable is False
        assert task.error_code == "UNSUPPORTED_SOURCE_FORMAT"


def test_prompt_version_change_enqueues_deferred_historical_recuration(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    with Session(queue.engine) as session:
        logical = LogicalItem(logical_key="done:versioned", title="合成事项")
        session.add(logical); session.flush()
        session.add(OAItem(
            oa_item_key="done:versioned", source_channel="done", title="合成事项",
            logical_item_id=logical.id,
        ))
        session.add(CuratedRun(
            logical_item_id=logical.id, input_signature="a" * 64, status="needs_review",
            rules_version="curation-rules-v1", prompt_version="curation-prompt-v1",
            schema_version="curation-schema-v1", model_name="qwen3.5:9b",
            config_signature="b" * 64,
        ))
        session.commit()

    assert queue.enqueue_stale_curation(
        rules_version="curation-rules-v2",
        prompt_version="curation-prompt-v2",
        schema_version="curation-schema-v1",
    ) == 1
    with Session(queue.engine) as session:
        task = session.scalar(select(PipelineTask).where(
            PipelineTask.logical_item_key == "done:versioned",
            PipelineTask.queue_name == "historical_done_backfill",
        ))
        assert task is not None
        assert task.stage == "curation"
        assert task.status == "queued"
