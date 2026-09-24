from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import ClassificationDecision, ClassificationRun
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, MarkdownExport, OAItem, OAManifestItem, ParseJob, PipelineEvent, PipelineTask
from oa_knowledge.done_convergence import DoneConvergencePlanner


def _planner(config_file: Path):
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    return DoneConvergencePlanner(engine, settings), engine


def _manifest(key: str, status: str, *, excluded: str | None = None, no_attachment: bool = False):
    return OAManifestItem(
        oa_item_key=key, title=f"synthetic {key}", list_page=1,
        processing_status=status, matched_exclusion_keyword=excluded,
        no_attachment_confirmed=no_attachment,
    )


def _task(key: str, queue: str, stage: str, status: str, *, recoverable: bool = True):
    return PipelineTask(
        queue_name=queue, priority=10 if queue == "realtime_done" else 50,
        logical_item_key=key, stage=stage, status=status,
        idempotency_key=f"synthetic:{key}:{queue}:{stage}", recoverable=recoverable,
        attempts=3, error_code="SYNTHETIC_FAILURE", last_error="synthetic",
    )


def test_planner_converges_only_eligible_items_and_is_idempotent(config_file: Path) -> None:
    planner, engine = _planner(config_file)
    with Session(engine) as session:
        session.add_all([
            _manifest("done:excluded", "skipped", excluded="synthetic"),
            _manifest("done:download", "pending_download"),
            _manifest("done:retry-download", "download_failed"),
            _manifest("done:markdown", "downloaded"),
            _manifest("done:terminal", "downloaded"),
            _manifest("done:active", "downloaded"),
            OAItem(oa_item_key="done:markdown", source_channel="done", title="synthetic", archive_relpath="originals/synthetic/item"),
            OAItem(oa_item_key="done:terminal", source_channel="done", title="synthetic", archive_relpath="originals/synthetic/terminal"),
            OAItem(oa_item_key="done:active", source_channel="done", title="synthetic", archive_relpath="originals/synthetic/active"),
            _task("done:retry-download", "realtime_done", "done_capture_and_archive", "failed"),
            _task("done:terminal", "markdown_delivery", "parse", "failed", recoverable=False),
            _task("done:active", "markdown_delivery", "attachment_inventory", "running"),
        ])
        session.commit()

    first = planner.plan(apply=True)
    second = planner.plan(apply=True)

    assert first.eligible == 5
    assert first.excluded == 1
    assert first.download_created == 1
    assert first.download_requeued == 1
    assert first.markdown_created == 1
    assert first.markdown_requeued == 0
    assert first.attention == 1
    assert second.download_created == second.download_requeued == 0
    assert second.markdown_created == second.markdown_requeued == 0
    assert second.attention == 1
    with Session(engine) as session:
        stages = list(session.execute(select(PipelineTask.logical_item_key, PipelineTask.stage, PipelineTask.status)))
        assert ("done:download", "done_capture_and_archive", "queued") in stages
        assert ("done:markdown", "attachment_inventory", "queued") in stages
        assert session.scalar(select(func.count(PipelineTask.id)).where(PipelineTask.logical_item_key == "done:excluded")) == 0
        retried = session.scalar(select(PipelineTask).where(PipelineTask.logical_item_key == "done:retry-download"))
        assert retried.status == "queued" and retried.attempts == 0 and retried.error_code is None
        assert session.scalar(select(func.count(PipelineEvent.id)).where(
            PipelineEvent.task_id == retried.id,
            PipelineEvent.event_type == "convergence_requeued",
        )) == 1


def test_planner_dry_run_reports_without_writing(config_file: Path) -> None:
    planner, engine = _planner(config_file)
    with Session(engine) as session:
        session.add(_manifest("done:dry-run", "pending_download"))
        session.commit()

    report = planner.plan(apply=False)

    assert report.download_created == 1
    with Session(engine) as session:
        assert session.scalar(select(func.count(PipelineTask.id))) == 0


def test_planner_requeues_legacy_daily_delivery_terminal_failure(config_file: Path) -> None:
    planner, engine = _planner(config_file)
    with Session(engine) as session:
        session.add_all([
            _manifest("done:legacy-partial", "downloaded"),
            OAItem(
                oa_item_key="done:legacy-partial", source_channel="done", title="synthetic",
                archive_relpath="originals/synthetic/legacy-partial",
            ),
            _task(
                "done:legacy-partial", "markdown_delivery", "source_publish", "failed",
                recoverable=False,
            ),
        ])
        session.flush()
        task = session.scalar(select(PipelineTask).where(
            PipelineTask.logical_item_key == "done:legacy-partial"
        ))
        task.error_code = "DAILY_DELIVERY_PARTIAL"
        session.commit()

    report = planner.plan(apply=True)

    assert report.markdown_requeued == 1
    assert report.attention == 0
    with Session(engine) as session:
        task = session.scalar(select(PipelineTask).where(
            PipelineTask.logical_item_key == "done:legacy-partial"
        ))
        assert task.status == "queued"
        assert task.recoverable is True
        assert task.error_code is None


def test_successful_item_index_needs_no_markdown_task(config_file: Path) -> None:
    planner, engine = _planner(config_file)
    with Session(engine) as session:
        item = OAItem(oa_item_key="done:complete", source_channel="done", title="synthetic", archive_relpath="originals/synthetic/complete")
        session.add_all([_manifest("done:complete", "no_attachment", no_attachment=True), item])
        session.flush()
        session.add(MarkdownExport(
            oa_item_id=item.id, document_kind="item_index", source_sha256="0" * 64,
            source_relpath="originals/synthetic/complete", markdown_relpath="knowledge/synthetic/complete.md",
            parse_engine="item_index", parse_engine_version="1", parse_config_hash="0" * 64,
            schema_version="1", status="success",
        ))
        session.commit()
    target = planner.settings.workspace_root / "knowledge/synthetic/complete.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# synthetic", encoding="utf-8")

    report = planner.plan(apply=True)

    assert report.markdown_created == 0
    assert report.attention == 0


def test_item_index_without_attachment_markdown_is_not_complete(config_file: Path) -> None:
    planner, engine = _planner(config_file)
    with Session(engine) as session:
        item = OAItem(
            oa_item_key="done:partial-index", source_channel="done", title="synthetic",
            archive_relpath="originals/synthetic/partial-index",
        )
        session.add_all([_manifest("done:partial-index", "downloaded"), item])
        session.flush()
        session.add(ArchivedFile(
            oa_item_id=item.id, original_name="synthetic.pdf", attachment_key="source",
            file_role="direct_attachment", source_container_key="synthetic",
            download_status="verified", local_relpath="originals/synthetic/partial-index/synthetic.pdf",
            sha256="1" * 64,
        ))
        session.add(MarkdownExport(
            oa_item_id=item.id, document_kind="item_index", source_sha256="0" * 64,
            source_relpath="originals/synthetic/partial-index", markdown_relpath="knowledge/synthetic/partial-index.md",
            parse_engine="item_index", parse_engine_version="1", parse_config_hash="0" * 64,
            schema_version="1", status="success",
        ))
        session.commit()

    report = planner.plan(apply=True)

    assert report.markdown_created == 1


def test_requeued_markdown_task_revives_failed_source_parse_jobs(config_file: Path) -> None:
    planner, engine = _planner(config_file)
    with Session(engine) as session:
        item = OAItem(
            oa_item_key="done:retry-markdown", source_channel="done", title="synthetic",
            archive_relpath="originals/synthetic/retry-markdown",
        )
        session.add_all([_manifest("done:retry-markdown", "downloaded"), item])
        session.flush()
        source = ArchivedFile(
            oa_item_id=item.id, original_name="synthetic.pdf", attachment_key="source",
            file_role="direct_attachment", source_container_key="synthetic",
            download_status="verified", local_relpath="originals/synthetic/retry-markdown/synthetic.pdf",
        )
        session.add(source)
        session.flush()
        job = ParseJob(
            file_id=source.id, engine="synthetic", engine_version="1", config_hash="synthetic",
            status="failed", attempts=3, error_code="parse_failed",
        )
        task = _task("done:retry-markdown", "markdown_delivery", "parse", "failed")
        session.add_all([job, task])
        session.commit()
        job_id = job.id

    report = planner.plan(apply=True)

    assert report.markdown_requeued == 1
    with Session(engine) as session:
        job = session.get(ParseJob, job_id)
        assert job.status == "queued"
        assert job.attempts == 0
        assert job.error_code is None


def test_confirmed_classification_resumes_completed_review_task(config_file: Path) -> None:
    planner, engine = _planner(config_file)
    with Session(engine) as session:
        session.add_all([
            _manifest("done:reviewed", "downloaded"),
            OAItem(
                oa_item_key="done:reviewed", source_channel="done", title="synthetic",
                archive_relpath="originals/synthetic/reviewed",
            ),
            _task("done:reviewed", "markdown_delivery", "classify", "completed"),
        ])
        run = ClassificationRun(
            run_id="synthetic-review", run_kind="incremental", status="completed",
            input_signature="a" * 64, manifest_sha256="b" * 64,
            exclusion_policy_sha256="c" * 64, rule_version="synthetic",
            schema_version="synthetic", prompt_version="synthetic",
            model_name="synthetic", private_config_sha256="d" * 64,
        )
        session.add(run)
        session.flush()
        session.add(ClassificationDecision(
            classification_run_id=run.id, oa_item_key="done:reviewed",
            version=1, is_current=True, decision_input_sha256="e" * 64,
            decision_source="manual", classification_status="classified",
            content_integrity_status="ok", content_origin="internal",
            initiator_type="internal", business_category="04_财务资金与融资",
            normalized_title="synthetic", classification_confidence=1.0,
            rule_version="synthetic", private_config_sha256="d" * 64,
            manual_locked=True, actor="synthetic-tester",
        ))
        session.commit()

    report = planner.plan(apply=True)

    assert report.markdown_requeued == 1
    with Session(engine) as session:
        task = session.scalar(select(PipelineTask).where(
            PipelineTask.logical_item_key == "done:reviewed"
        ))
        assert task.status == "queued"
        assert task.stage == "classify"

def test_missing_published_markdown_is_requeued_without_redownload(config_file: Path) -> None:
    planner, engine = _planner(config_file)
    with Session(engine) as session:
        item = OAItem(oa_item_key="done:missing-output", source_channel="done", title="synthetic",
                      archive_relpath="originals/synthetic/missing-output")
        session.add_all([_manifest("done:missing-output", "no_attachment", no_attachment=True), item])
        session.flush()
        session.add(MarkdownExport(
            oa_item_id=item.id, document_kind="item_index", source_sha256="0" * 64,
            source_relpath="originals/synthetic/missing-output",
            markdown_relpath="knowledge/synthetic/missing-output.md",
            parse_engine="item_index", parse_engine_version="1", parse_config_hash="0" * 64,
            schema_version="1", status="success",
        ))
        session.commit()

    report = planner.plan(apply=True)

    assert report.markdown_created == 1
    assert report.download_created == 0
