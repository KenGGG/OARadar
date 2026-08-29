from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from oa_knowledge.classification.semantic_qa import write_semantic_run_reports
from oa_knowledge.db.models import (
    Base,
    ClassificationDecision,
    ClassificationRun,
    ClassificationRunItem,
)


def _decision(session: Session, run: ClassificationRun, key: str, *, status: str, current: bool, version: int, supersedes: int | None = None) -> ClassificationDecision:
    row = ClassificationDecision(
        classification_run_id=run.id, oa_item_key=key, version=version,
        is_current=current, decision_input_sha256="a" * 64,
        decision_source="agnes", classification_status=status,
        content_integrity_status="ok", content_origin="external" if status == "classified" else None,
        initiator_type="internal", canonical_issuer="Synthetic Authority" if status == "classified" else None,
        normalized_title=f"Synthetic {key}", classification_confidence=0.96,
        classification_reason_json="{}", rule_version="test", private_config_sha256="b" * 64,
        supersedes_decision_id=supersedes,
    )
    session.add(row); session.flush()
    return row


def test_semantic_run_report_writes_terminal_counts_and_review_list(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        prior = ClassificationRun(run_id="prior", run_kind="full", status="completed", input_signature="a" * 64, manifest_sha256="a" * 64, exclusion_policy_sha256="a" * 64, rule_version="r", schema_version="s", prompt_version="p", model_name="local", private_config_sha256="a" * 64, target_count=2, excluded_count=0)
        semantic = ClassificationRun(run_id="semantic", run_kind="incremental", status="completed", input_signature="b" * 64, manifest_sha256="b" * 64, exclusion_policy_sha256="b" * 64, rule_version="semantic-v2", schema_version="classification-v1", prompt_version="agnes-classifier-v1.1", model_name="agnes+qwen", private_config_sha256="b" * 64, target_count=2, excluded_count=0)
        session.add_all((prior, semantic)); session.flush()
        old = _decision(session, prior, "done:changed", status="needs_review", current=False, version=1)
        changed = _decision(session, semantic, "done:changed", status="classified", current=True, version=2, supersedes=old.id)
        review = _decision(session, prior, "done:review", status="needs_review", current=True, version=1)
        session.add_all((
            ClassificationRunItem(classification_run_id=semantic.id, oa_item_key="done:changed", inclusion_reason="semantic_review", stage="decided", adopted_decision_id=changed.id, last_error_detail=json.dumps({"provider":"agnes","result":"classified","cache_hit":False})),
            ClassificationRunItem(classification_run_id=semantic.id, oa_item_key="done:review", inclusion_reason="semantic_review", stage="decided", adopted_decision_id=review.id, last_error_detail=json.dumps({"provider":"local_qwen","result":"rejected","rejection_code":"schema_invalid","prior_attempts":[{"provider":"local_qwen","result":"rejected","rejection_code":"schema_invalid"}]})),
        ))

    report = write_semantic_run_reports(factory, "semantic", tmp_path)

    assert report["target_total"] == 2
    assert report["classified"] == 1
    assert report["needs_review"] == 1
    assert report["newly_classified_from_review"] == 1
    assert report["model_calls"]["agnes"] == 1
    assert report["model_calls"]["local_qwen"] == 2
    assert (tmp_path / "agnes_run_report.json").is_file()
    rows = (tmp_path / "remaining_needs_review.csv").read_text(encoding="utf-8")
    assert "done:review" in rows
    engine.dispose()
