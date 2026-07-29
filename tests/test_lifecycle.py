import json
from pathlib import Path

from sqlalchemy.orm import Session

from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import LogicalItem, SummaryEvidence, SummaryVersion
from oa_knowledge.lifecycle import (
    create_logical_item,
    create_summary_candidate,
    mark_dependent_summaries_stale,
    record_occurrence,
    record_snapshot,
    validate_summary_candidate,
)


def _session(tmp_path: Path) -> Session:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    return Session(create_db_engine(db))


def test_pending_and_done_occurrences_keep_distinct_workitem_ids(tmp_path: Path) -> None:
    with _session(tmp_path) as session:
        logical = create_logical_item(session, "stable-process-1", "Synthetic")
        pending = record_occurrence(session, logical.id, "pending:11", "pending", workitem_id_text="11", process_id_text="p1")
        done = record_occurrence(session, logical.id, "done:99", "done", workitem_id_text="99", process_id_text="p1")

        assert pending.logical_item_id == done.logical_item_id
        assert pending.workitem_id_text != done.workitem_id_text


def test_duplicate_titles_are_not_automatically_merged(tmp_path: Path) -> None:
    with _session(tmp_path) as session:
        first = create_logical_item(session, "stable-a", "Same title")
        second = create_logical_item(session, "stable-b", "Same title")

        assert first.id != second.id


def test_pending_and_done_summaries_have_separate_current_pointers(tmp_path: Path) -> None:
    with _session(tmp_path) as session:
        logical = create_logical_item(session, "stable", "Synthetic")
        occurrence = record_occurrence(session, logical.id, "pending:1", "pending", process_id_text="p1")
        pending_snapshot = record_snapshot(session, logical.id, occurrence.id, "pending_initial", {"body": "pending"})
        done_snapshot = record_snapshot(session, logical.id, occurrence.id, "done_final", {"body": "done"})
        candidates = []
        for kind, snapshot in (("pending_assist", pending_snapshot), ("done_official", done_snapshot)):
            candidate = create_summary_candidate(
                session, logical.id, snapshot.id, kind,
                {"summary": kind, "key_points": [], "attachment_summaries": []},
                provider_name="fake", model_name="synthetic", prompt_version="v1",
            )
            session.add(SummaryEvidence(
                summary_version_id=candidate.id, snapshot_id=snapshot.id, evidence_kind="snapshot",
                locator="body#^synthetic", evidence_hash="e" * 64,
            ))
            session.flush()
            assert validate_summary_candidate(session, candidate.id)
            candidates.append(candidate)

        session.refresh(logical)
        assert logical.current_pending_summary_id == candidates[0].id
        assert logical.current_done_summary_id == candidates[1].id


def test_candidate_without_evidence_cannot_replace_current_summary(tmp_path: Path) -> None:
    with _session(tmp_path) as session:
        logical = create_logical_item(session, "stable", "Synthetic")
        occurrence = record_occurrence(session, logical.id, "pending:1", "pending")
        snapshot = record_snapshot(session, logical.id, occurrence.id, "pending_initial", {"body": "pending"})
        candidate = create_summary_candidate(
            session, logical.id, snapshot.id, "pending_assist",
            {"summary": "text", "key_points": [], "attachment_summaries": []},
            provider_name="fake", model_name="synthetic", prompt_version="v1",
        )

        assert not validate_summary_candidate(session, candidate.id)
        assert candidate.status == "candidate"


def test_new_snapshot_marks_only_dependent_summary_kind_stale(tmp_path: Path) -> None:
    with _session(tmp_path) as session:
        logical = create_logical_item(session, "stable", "Synthetic")
        occurrence = record_occurrence(session, logical.id, "pending:1", "pending")
        old = record_snapshot(session, logical.id, occurrence.id, "pending_initial", {"body": "v1"})
        candidate = create_summary_candidate(
            session, logical.id, old.id, "pending_assist",
            {"summary": "v1", "key_points": [], "attachment_summaries": []},
            provider_name="fake", model_name="synthetic", prompt_version="v1",
        )
        session.add(SummaryEvidence(summary_version_id=candidate.id, snapshot_id=old.id, evidence_kind="snapshot", locator="body", evidence_hash="f" * 64))
        session.flush()
        assert validate_summary_candidate(session, candidate.id)
        new = record_snapshot(session, logical.id, occurrence.id, "pending_updated", {"body": "v2"})

        changed = mark_dependent_summaries_stale(session, logical.id, "pending_assist", new.id)

        assert changed == 1
        assert candidate.status == "stale"
        session.refresh(logical)
        assert logical.current_pending_summary_id is None
        assert json.loads(new.payload_json) == {"body": "v2"}
