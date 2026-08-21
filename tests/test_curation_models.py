from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import CuratedDecision, CuratedDecisionSource, CuratedRun, LogicalItem


def test_curated_migration_adds_versioned_run_and_decision_tables(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)

    with sqlite3.connect(db) as connection:
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "0036_rebuild_classification_gate"
        )
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    assert {"curated_runs", "curated_decisions", "curated_decision_sources"} <= tables


def test_curated_run_signature_and_decision_source_membership_are_unique(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)

    with Session(engine) as session:
        logical = LogicalItem(logical_key="synthetic-package", title="Synthetic")
        session.add(logical)
        session.flush()
        run = CuratedRun(
            logical_item_id=logical.id, input_signature="a" * 64, status="planned",
            rules_version="rules-v1", prompt_version="prompt-v1", schema_version="schema-v1",
            model_name="qwen3.5:9b", config_signature="b" * 64,
        )
        session.add(run)
        session.flush()
        decision = CuratedDecision(
            curated_run_id=run.id, ordinal=1, status="candidate", document_kind="formal",
            canonical_key="formal:synthetic-001", normalized_title="Synthetic notice",
            metadata_json="{}", confidence=0.9, decision_hash="c" * 64,
        )
        session.add(decision)
        session.flush()
        session.add(CuratedDecisionSource(
            curated_decision_id=decision.id, source_key="source-1", ordinal=1,
            role="body", content_sha256="d" * 64,
        ))
        session.commit()

        session.add(CuratedRun(
            logical_item_id=logical.id, input_signature="a" * 64, status="planned",
            rules_version="rules-v1", prompt_version="prompt-v1", schema_version="schema-v1",
            model_name="qwen3.5:9b", config_signature="b" * 64,
        ))
        with pytest.raises(IntegrityError):
            session.commit()
