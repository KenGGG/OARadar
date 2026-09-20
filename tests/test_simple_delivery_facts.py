"""Synthetic database facts must not turn an index into complete delivery."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from oa_knowledge.config import load_settings
from oa_knowledge.db.models import (
    ArchivedFile, Base, ClassificationDecision, ClassificationRun,
    MarkdownExport, MarkdownTask, OAItem, OAManifestItem, ParseJob,
)
from oa_knowledge.web.simple_status import (
    _attention_list, _done_simple_status_map, _done_summary, _pending_summary,
)
from oa_knowledge.web.delivery_facts import delivery_facts


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value
    engine.dispose()


def _item(session, key="synthetic", processing_status="downloaded"):
    item = OAItem(oa_item_key=key, title="合成事项", source_channel="done",
                  pipeline_status=processing_status)
    manifest = OAManifestItem(oa_item_key=key, title="合成事项", list_page=1,
                              processing_status=processing_status, no_attachment_confirmed=processing_status == "no_attachment")
    session.add_all([item, manifest])
    session.flush()
    return item, manifest


def _file(session, item, key="one"):
    file = ArchivedFile(oa_item_id=item.id, original_name="synthetic.pdf",
                        attachment_key=key, file_role="direct_attachment",
                        source_container_key="synthetic", download_status="downloaded")
    session.add(file)
    session.flush()
    return file


def _export(session, item, file=None, status="success", legacy=False):
    export = MarkdownExport(
        oa_item_id=None if legacy else item.id,
        source_file_id=file.id if file else None,
        document_kind="attachment" if file else "item_index",
        source_sha256="0" * 64, source_relpath="synthetic/source",
        markdown_relpath=f"synthetic/{item.id}/{file.id if file else 'index'}.md",
        parse_engine="synthetic", parse_engine_version="v1", parse_config_hash="synthetic",
        schema_version="v1", status=status,
    )
    session.add(export)
    session.flush()
    return export


@pytest.mark.parametrize("failure", ["missing", "export", "parse", "task", "unsupported"])
def test_successful_index_cannot_hide_missing_or_failed_source(session, failure):
    item, manifest = _item(session)
    file = _file(session, item)
    _export(session, item)
    if failure in {"export", "unsupported"}:
        _export(session, item, file, status="failed" if failure == "export" else "unsupported")
    elif failure == "parse":
        session.add(ParseJob(file_id=file.id, engine="synthetic", engine_version="v1",
                             config_hash="synthetic", status="failed"))
    elif failure == "task":
        session.add(MarkdownTask(source_file_id=file.id, schema_version="v1", status="failed"))
    session.flush()

    state = _done_simple_status_map(session)[manifest.id][0]
    assert state == ("waiting_markdown" if failure == "missing" else "attention")
    assert _done_summary(session, {})["published_items"] == 0


def test_index_without_attachment_evidence_does_not_claim_completion(session):
    item, manifest = _item(session)
    _export(session, item)
    assert _done_simple_status_map(session)[manifest.id][0] == "waiting_markdown"


def test_legacy_attachment_export_and_index_can_complete(session):
    item, manifest = _item(session)
    file = _file(session, item)
    _export(session, item, file, legacy=True)
    _export(session, item)
    assert _done_simple_status_map(session)[manifest.id][0] == "completed"


def test_confirmed_no_attachment_can_complete_with_index(session):
    item, manifest = _item(session, processing_status="no_attachment")
    _export(session, item)
    assert _done_simple_status_map(session)[manifest.id][0] == "completed"


def test_excluded_records_do_not_prevent_eligible_completion(session):
    item, _ = _item(session)
    _export(session, item, _file(session, item))
    _export(session, item)
    _item(session, "excluded", "skipped")
    summary = _done_summary(session, {})
    assert summary["status"] == "completed"
    assert summary["excluded"] == 1


def test_enabled_pending_without_execution_is_not_success(session, config_file):
    settings = load_settings(config_file)
    settings.feishu.enabled = True
    settings.llm.enabled = True
    summary = _pending_summary(session, settings, {})
    assert summary["status"] == "not_run"
    assert "发送成功" not in summary["headline"]


def test_disabled_pending_is_explicit(session, config_file):
    summary = _pending_summary(session, load_settings(config_file), {})
    assert summary["status"] == "disabled"
    assert summary["feishu_enabled"] is False
    assert summary["model_enabled"] is False


def test_pending_alerts_route_to_affected_records():
    alerts = _attention_list({"failed_items": 1}, {
        "feishu_failed": 2, "feishu_unknown": 3, "model_failed": 4,
    })
    assert [(alert["jump"], alert.get("filter")) for alert in alerts] == [
        ("done", "attention"), ("pending", "feishu_failed"),
        ("pending", "feishu_unknown"), ("pending", "summary_failed"),
    ]


@pytest.mark.parametrize("classification", ["needs_review", "excluded"])
def test_classification_blocks_complete_delivery(session, classification):
    item, manifest = _item(session)
    _export(session, item, _file(session, item))
    _export(session, item)
    run = ClassificationRun(
        run_id="synthetic", run_kind="full", input_signature="0" * 64,
        manifest_sha256="0" * 64, exclusion_policy_sha256="0" * 64,
        rule_version="v1", schema_version="v1", prompt_version="v1",
        model_name="synthetic", private_config_sha256="0" * 64,
    )
    session.add(run)
    session.flush()
    session.add(ClassificationDecision(
        classification_run_id=run.id, oa_item_key=item.oa_item_key,
        version=1, is_current=True, decision_input_sha256="0" * 64,
        decision_source="metadata_rule", classification_status=classification,
        content_integrity_status="ok", initiator_type="unknown",
        normalized_title="合成事项", classification_confidence=0,
        rule_version="v1", private_config_sha256="0" * 64,
    ))
    session.flush()
    facts = delivery_facts(session, item)
    assert facts["status"] == classification
    assert facts["classification_status"] == classification
    assert _done_simple_status_map(session)[manifest.id][0] == (
        "attention" if classification == "needs_review" else "excluded"
    )
    if classification == "needs_review":
        summary = _done_summary(session, {})
        assert summary["review_items"] == 1
        assert summary["failed_items"] == 0


def test_delivery_facts_count_attachments_and_not_index_as_success(session):
    item, _ = _item(session)
    _export(session, item, _file(session, item), legacy=True)
    _export(session, item, _file(session, item, "two"), status="unsupported")
    _file(session, item, "three")
    _export(session, item)
    facts = delivery_facts(session, item)
    assert facts["status"] == "partial"
    assert (facts["expected"], facts["successful"], facts["unsupported"], facts["failed"]) == (3, 1, 1, 0)
    assert facts["index_status"] == "success"
    assert facts["index_relpath"].endswith("/index.md")


def test_depth_limit_cannot_be_complete_even_with_all_exports(session):
    item, manifest = _item(session, processing_status="depth_limit_reached")
    _export(session, item, _file(session, item))
    _export(session, item)
    assert delivery_facts(session, item)["status"] == "needs_review"
    summary = _done_summary(session, {})
    assert summary["archive_complete"] == 0
    assert summary["download_issue_items"] == 1
    assert _done_simple_status_map(session)[manifest.id][0] == "attention"


def test_authentication_failure_is_actionable_not_a_download_queue(session):
    _, manifest = _item(session, processing_status="auth_required")
    assert _done_simple_status_map(session)[manifest.id][0] == "attention"


def test_archive_and_markdown_summaries_show_independent_outcomes(session, config_file):
    from oa_knowledge.web import simple_status as status_module
    item, _ = _item(session)
    _export(session, item, _file(session, item), status="unsupported")
    _export(session, item)
    _item(session, "excluded", "skipped")
    summaries = getattr(status_module, "_workflow_summaries", lambda *args: {})(
        session, load_settings(config_file), {"hourly_enabled": True, "next_run_at": "synthetic-next"},
    )
    assert "archive" in summaries and "markdown" in summaries
    assert summaries["archive"]["status"] == "completed"
    assert summaries["archive"]["complete"] == 1
    assert summaries["archive"]["excluded"] == 1
    assert summaries["markdown"]["status"] == "attention"
    assert summaries["markdown"]["complete"] == 0
    assert summaries["markdown"]["partial"] == 1
    assert summaries["markdown"]["unsupported_files"] == 1
    assert summaries["archive"]["next_run_at"] == "synthetic-next"


def test_success_of_one_pending_stage_does_not_claim_both_succeeded(session, config_file):
    settings = load_settings(config_file)
    settings.feishu.enabled = settings.llm.enabled = True
    summary = _pending_summary(session, settings, {"notifications": {"counts": {"sent": 1}}})
    assert summary["status"] == "working"
    assert "运行正常" not in summary["headline"]


def test_done_summary_counts_running_tasks_separately_from_waiting(session):
    item, _ = _item(session)
    file = _file(session, item)
    session.add(MarkdownTask(source_file_id=file.id, schema_version="v1", status="running"))
    session.flush()
    summary = _done_summary(session, {})
    assert summary["running_items"] == 1
    assert summary["queued_items"] == 0


def test_successful_current_export_supersedes_an_old_failed_parse(session):
    item, _ = _item(session)
    file = _file(session, item)
    session.add(ParseJob(file_id=file.id, engine="old", engine_version="1", config_hash="old", status="failed"))
    _export(session, item, file)
    _export(session, item)
    assert delivery_facts(session, item)["status"] == "complete"


def test_skipped_parse_is_partial_even_when_index_is_successful(session):
    item, _ = _item(session)
    file = _file(session, item)
    session.add(ParseJob(file_id=file.id, engine="old", engine_version="1", config_hash="old", status="skipped"))
    _export(session, item)
    assert delivery_facts(session, item)["unsupported"] == 1


def test_changed_original_does_not_reuse_stale_successful_export(session):
    item, _ = _item(session)
    file = _file(session, item)
    file.sha256 = "1" * 64
    _export(session, item, file)
    _export(session, item)
    assert delivery_facts(session, item)["status"] != "complete"
    assert delivery_facts(session, item)["successful"] == 0


def test_unconfirmed_empty_inventory_needs_review_even_with_index(session):
    item, manifest = _item(session, processing_status="no_attachment")
    manifest.no_attachment_confirmed = False
    _export(session, item)
    assert delivery_facts(session, item)["status"] == "needs_review"
    summary = _done_summary(session, {})
    assert summary["archive_complete"] == 0
    assert summary["download_issue_items"] == 1
