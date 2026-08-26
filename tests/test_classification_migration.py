from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db import models


CLASSIFICATION_TABLES = {
    "classification_runs",
    "classification_run_items",
    "classification_decisions",
    "classification_evidence",
}


def _alembic_config(database_path: Path) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(root / "src/oa_knowledge/db/migrations"),
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    return config


def test_0038_upgrade_and_downgrade_are_exact(tmp_path: Path) -> None:
    database_path = tmp_path / "classification.db"
    upgrade_database(database_path)

    engine = create_db_engine(database_path)
    inspector = inspect(engine)
    assert CLASSIFICATION_TABLES <= set(inspector.get_table_names())
    parse_columns = {column["name"]: column for column in inspector.get_columns("parse_artifacts")}
    assert parse_columns["profile_version"]["nullable"] is False
    parse_indexes = {index["name"]: index for index in inspector.get_indexes("parse_artifacts")}
    assert parse_indexes["uq_parse_artifact_reuse_identity"]["unique"] == 1
    assert parse_indexes["uq_parse_artifact_reuse_identity"]["column_names"] == [
        "content_object_id",
        "engine",
        "engine_version",
        "profile_version",
        "config_hash",
    ]
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            "0038_oa_markdown_v1_classification"
        )

    engine.dispose()
    command.downgrade(_alembic_config(database_path), "0037_no_attachment_evidence")

    downgraded = create_db_engine(database_path)
    downgraded_inspector = inspect(downgraded)
    assert CLASSIFICATION_TABLES.isdisjoint(downgraded_inspector.get_table_names())
    assert "profile_version" not in {
        column["name"] for column in downgraded_inspector.get_columns("parse_artifacts")
    }
    assert "uq_parse_artifact_reuse_identity" not in {
        index["name"] for index in downgraded_inspector.get_indexes("parse_artifacts")
    }


def test_classification_models_preserve_text_keys_and_current_decision(tmp_path: Path) -> None:
    for name in (
        "ClassificationRun",
        "ClassificationRunItem",
        "ClassificationDecision",
        "ClassificationEvidence",
    ):
        assert hasattr(models, name)

    database_path = tmp_path / "classification.db"
    upgrade_database(database_path)
    engine = create_db_engine(database_path)
    long_key = "done:-922337203685477580812345678901234567890"

    with Session(engine) as session:
        run = models.ClassificationRun(
            run_id="dry-run-synthetic",
            run_kind="full",
            status="created",
            input_signature="a" * 64,
            manifest_sha256="b" * 64,
            exclusion_policy_sha256="c" * 64,
            rule_version="rules-v1",
            schema_version="schema-v1",
            prompt_version="prompt-v1",
            model_name="synthetic-local",
            private_config_sha256="d" * 64,
            target_count=1,
            excluded_count=0,
            summary_json="{}",
        )
        session.add(run)
        session.flush()
        session.add(models.ClassificationRunItem(
            classification_run_id=run.id,
            oa_item_key=long_key,
            inclusion_reason="classification_target",
            stage="queued",
            attempts=0,
        ))
        first = models.ClassificationDecision(
            classification_run_id=run.id,
            oa_item_key=long_key,
            version=1,
            is_current=True,
            decision_input_sha256="e" * 64,
            decision_source="metadata_rule",
            classification_status="classified",
            content_integrity_status="ok",
            content_origin="internal",
            flow_type="internal_relay",
            initiator_type="mixed",
            transfer_chain_json="[]",
            business_category="01_公司治理与决策",
            normalized_title="Synthetic title",
            classification_confidence=0.95,
            classification_reason_json="{}",
            rule_version="rules-v1",
            private_config_sha256="d" * 64,
            manual_locked=False,
        )
        session.add(first)
        session.flush()
        session.add(models.ClassificationEvidence(
            classification_decision_id=first.id,
            sequence=1,
            evidence_type="title_template",
            evidence_scope="package",
            value_json='{"rule":"synthetic"}',
            confidence=0.95,
        ))
        session.commit()

        assert session.get(models.ClassificationRunItem, 1).oa_item_key == long_key
        duplicate_current = models.ClassificationDecision(
            classification_run_id=run.id,
            oa_item_key=long_key,
            version=2,
            is_current=True,
            decision_input_sha256="f" * 64,
            decision_source="manual",
            classification_status="classified",
            content_integrity_status="ok",
            content_origin="internal",
            initiator_type="internal",
            transfer_chain_json="[]",
            business_category="99_其他内部",
            normalized_title="Synthetic title",
            classification_confidence=1.0,
            classification_reason_json="{}",
            rule_version="rules-v1",
            private_config_sha256="d" * 64,
            manual_locked=True,
            supersedes_decision_id=first.id,
        )
        session.add(duplicate_current)
        with pytest.raises(IntegrityError):
            session.commit()


def test_classification_constraints_keep_status_axes_and_package_fields_separate(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "classification.db"
    upgrade_database(database_path)
    engine = create_db_engine(database_path)
    with Session(engine) as session:
        run = models.ClassificationRun(
            run_id="constraints",
            run_kind="incremental",
            status="running",
            input_signature="1" * 64,
            manifest_sha256="2" * 64,
            exclusion_policy_sha256="3" * 64,
            rule_version="rules-v1",
            schema_version="schema-v1",
            prompt_version="prompt-v1",
            model_name="synthetic-local",
            private_config_sha256="4" * 64,
            target_count=1,
            excluded_count=0,
            summary_json="{}",
        )
        session.add(run)
        session.commit()
        run_id = run.id

    with Session(engine) as session:
        invalid = models.ClassificationDecision(
            classification_run_id=run_id,
            oa_item_key="done:synthetic",
            version=1,
            is_current=True,
            decision_input_sha256="5" * 64,
            decision_source="metadata_rule",
            classification_status="classified",
            content_integrity_status="download_failed",
            content_origin="external",
            initiator_type="external",
            transfer_chain_json="[]",
            issuer="Synthetic Authority",
            canonical_issuer="Synthetic Authority",
            business_category="99_其他内部",
            normalized_title="Synthetic title",
            classification_confidence=0.9,
            classification_reason_json="{}",
            rule_version="rules-v1",
            private_config_sha256="4" * 64,
            manual_locked=False,
        )
        session.add(invalid)
        with pytest.raises(IntegrityError):
            session.commit()

    with Session(engine) as session:
        run = session.query(models.ClassificationRun).one()
        excluded = models.ClassificationDecision(
            classification_run_id=run.id,
            oa_item_key="done:excluded",
            version=1,
            is_current=True,
            decision_input_sha256="6" * 64,
            decision_source="metadata_rule",
            classification_status="excluded",
            content_integrity_status="not_checked",
            content_origin=None,
            initiator_type="unknown",
            transfer_chain_json="[]",
            normalized_title="Synthetic excluded",
            classification_confidence=1.0,
            classification_reason_json="{}",
            rule_version="rules-v1",
            private_config_sha256="4" * 64,
            manual_locked=False,
        )
        session.add(excluded)
        session.commit()
        assert excluded.classification_status == "excluded"
        assert excluded.content_integrity_status == "not_checked"
