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


def _parse_schema_snapshot(database_path: Path) -> dict[str, object]:
    with sqlite3.connect(database_path) as connection:
        indexes = connection.execute("PRAGMA index_list(parse_artifacts)").fetchall()
        return {
            "columns": connection.execute("PRAGMA table_info(parse_artifacts)").fetchall(),
            "foreign_keys": sorted(
                (row[2], row[3], row[4], row[5], row[6], row[7])
                for row in connection.execute(
                    "PRAGMA foreign_key_list(parse_artifacts)"
                ).fetchall()
            ),
            "indexes": sorted(
                (
                    row[1],
                    row[2],
                    tuple(
                        column[2]
                        for column in connection.execute(
                            f'PRAGMA index_info("{row[1]}")'
                        ).fetchall()
                    ),
                )
                for row in indexes
            ),
        }


def _insert_legacy_parse_artifacts(
    connection: sqlite3.Connection,
    *,
    count: int,
    content_sha256: str,
    config_hash: str,
) -> list[int]:
    item_id = connection.execute(
        """
        INSERT INTO oa_items (
            oa_item_key, source_channel, title, pipeline_status,
            first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (f"done:{content_sha256[:8]}", "done", "Synthetic parse item", "archived"),
    ).lastrowid
    content_object_id = connection.execute(
        "INSERT INTO content_objects (sha256, created_at) VALUES (?, CURRENT_TIMESTAMP)",
        (content_sha256,),
    ).lastrowid
    artifact_ids: list[int] = []
    for offset in range(count):
        file_id = connection.execute(
            """
            INSERT INTO files (
                oa_item_id, original_name, attachment_key, file_role,
                source_container_key, depth, download_status, download_attempts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                f"synthetic-{offset}.pdf",
                f"attachment-{offset}",
                "attachment",
                "root",
                1,
                "verified",
                0,
            ),
        ).lastrowid
        parse_job_id = connection.execute(
            """
            INSERT INTO parse_jobs (
                file_id, engine, engine_version, config_hash, status, attempts
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (file_id, "synthetic", "1", config_hash, "completed", 1),
        ).lastrowid
        artifact_id = connection.execute(
            """
            INSERT INTO parse_artifacts (
                parse_job_id, content_object_id, engine, engine_version,
                output_relpath, source_sha256, config_hash, page_map_json,
                lifecycle_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                parse_job_id,
                content_object_id,
                "synthetic",
                "1",
                f"markdown/cache/artifact-{offset}.md",
                content_sha256,
                config_hash,
                "{}",
                "valid",
            ),
        ).lastrowid
        artifact_ids.append(artifact_id)
    return artifact_ids


def test_0038_upgrade_and_downgrade_are_exact(tmp_path: Path) -> None:
    database_path = tmp_path / "classification.db"
    upgrade_database(database_path)
    command.downgrade(_alembic_config(database_path), "0037_no_attachment_evidence")
    before = _parse_schema_snapshot(database_path)

    with sqlite3.connect(database_path) as connection:
        _insert_legacy_parse_artifacts(
            connection,
            count=1,
            content_sha256="a" * 64,
            config_hash="c" * 64,
        )

    command.upgrade(_alembic_config(database_path), "0038_oa_markdown_v1_classification")

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
    downgraded.dispose()
    assert _parse_schema_snapshot(database_path) == before


def test_0038_preserves_legacy_parse_duplicates_with_deterministic_profiles(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-duplicates.db"
    upgrade_database(database_path)
    command.downgrade(_alembic_config(database_path), "0037_no_attachment_evidence")

    with sqlite3.connect(database_path) as connection:
        artifact_ids = _insert_legacy_parse_artifacts(
            connection,
            count=2,
            content_sha256="d" * 64,
            config_hash="e" * 64,
        )

    command.upgrade(_alembic_config(database_path), "0038_oa_markdown_v1_classification")

    with sqlite3.connect(database_path) as connection:
        profiles = connection.execute(
            "SELECT id, profile_version FROM parse_artifacts ORDER BY id"
        ).fetchall()
    assert profiles == [
        (artifact_ids[0], "legacy"),
        (artifact_ids[1], f"legacy-duplicate-{artifact_ids[1]}"),
    ]


def test_0038_prefers_active_legacy_parse_artifact_as_canonical(tmp_path: Path) -> None:
    database_path = tmp_path / "active-legacy-duplicate.db"
    upgrade_database(database_path)
    command.downgrade(_alembic_config(database_path), "0037_no_attachment_evidence")

    with sqlite3.connect(database_path) as connection:
        artifact_ids = _insert_legacy_parse_artifacts(
            connection,
            count=2,
            content_sha256="f" * 64,
            config_hash="1" * 64,
        )
        connection.execute(
            "UPDATE content_objects SET active_parse_artifact_id = ? WHERE sha256 = ?",
            (artifact_ids[1], "f" * 64),
        )

    command.upgrade(_alembic_config(database_path), "0038_oa_markdown_v1_classification")

    with sqlite3.connect(database_path) as connection:
        profiles = connection.execute(
            "SELECT id, profile_version FROM parse_artifacts ORDER BY id"
        ).fetchall()
    assert profiles == [
        (artifact_ids[0], f"legacy-duplicate-{artifact_ids[0]}"),
        (artifact_ids[1], "legacy"),
    ]


@pytest.mark.parametrize("set_invalid_active", [False, True])
def test_0038_prefers_valid_legacy_artifact_over_invalid_history(
    tmp_path: Path,
    set_invalid_active: bool,
) -> None:
    database_path = tmp_path / f"valid-legacy-{set_invalid_active}.db"
    upgrade_database(database_path)
    command.downgrade(_alembic_config(database_path), "0037_no_attachment_evidence")

    with sqlite3.connect(database_path) as connection:
        artifact_ids = _insert_legacy_parse_artifacts(
            connection,
            count=2,
            content_sha256="4" * 64,
            config_hash="5" * 64,
        )
        connection.execute(
            "UPDATE parse_artifacts SET lifecycle_status = 'rejected' WHERE id = ?",
            (artifact_ids[0],),
        )
        if set_invalid_active:
            connection.execute(
                "UPDATE content_objects SET active_parse_artifact_id = ? WHERE sha256 = ?",
                (artifact_ids[0], "4" * 64),
            )

    command.upgrade(_alembic_config(database_path), "0038_oa_markdown_v1_classification")

    with sqlite3.connect(database_path) as connection:
        profiles = connection.execute(
            "SELECT id, profile_version FROM parse_artifacts ORDER BY id"
        ).fetchall()
    assert profiles == [
        (artifact_ids[0], f"legacy-duplicate-{artifact_ids[0]}"),
        (artifact_ids[1], "legacy"),
    ]


def test_0038_uses_stable_minimum_when_no_legacy_artifact_is_valid(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "no-valid-legacy.db"
    upgrade_database(database_path)
    command.downgrade(_alembic_config(database_path), "0037_no_attachment_evidence")

    with sqlite3.connect(database_path) as connection:
        artifact_ids = _insert_legacy_parse_artifacts(
            connection,
            count=2,
            content_sha256="6" * 64,
            config_hash="7" * 64,
        )
        connection.execute("UPDATE parse_artifacts SET lifecycle_status = 'rejected'")
        connection.execute(
            "UPDATE content_objects SET active_parse_artifact_id = ? WHERE sha256 = ?",
            (artifact_ids[1], "6" * 64),
        )

    command.upgrade(_alembic_config(database_path), "0038_oa_markdown_v1_classification")

    with sqlite3.connect(database_path) as connection:
        profiles = connection.execute(
            "SELECT id, profile_version FROM parse_artifacts ORDER BY id"
        ).fetchall()
    assert profiles == [
        (artifact_ids[0], "legacy"),
        (artifact_ids[1], f"legacy-duplicate-{artifact_ids[1]}"),
    ]


def test_0038_recovers_when_profile_column_exists_without_reuse_index(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "interrupted-profile-backfill.db"
    upgrade_database(database_path)
    command.downgrade(_alembic_config(database_path), "0037_no_attachment_evidence")

    with sqlite3.connect(database_path) as connection:
        artifact_ids = _insert_legacy_parse_artifacts(
            connection,
            count=2,
            content_sha256="2" * 64,
            config_hash="3" * 64,
        )
        connection.execute(
            """
            ALTER TABLE parse_artifacts
            ADD COLUMN profile_version VARCHAR(80) NOT NULL DEFAULT 'legacy'
            """
        )

    command.upgrade(_alembic_config(database_path), "0038_oa_markdown_v1_classification")

    engine = create_db_engine(database_path)
    indexes = {index["name"] for index in inspect(engine).get_indexes("parse_artifacts")}
    assert "uq_parse_artifact_reuse_identity" in indexes
    with sqlite3.connect(database_path) as connection:
        profiles = connection.execute(
            "SELECT id, profile_version FROM parse_artifacts ORDER BY id"
        ).fetchall()
    assert profiles == [
        (artifact_ids[0], "legacy"),
        (artifact_ids[1], f"legacy-duplicate-{artifact_ids[1]}"),
    ]


def test_parse_artifact_model_declares_versioned_reuse_identity() -> None:
    indexes = {index.name: index for index in models.ParseArtifact.__table__.indexes}
    reuse_index = indexes["uq_parse_artifact_reuse_identity"]
    assert reuse_index.unique is True
    assert [column.name for column in reuse_index.columns] == [
        "content_object_id",
        "engine",
        "engine_version",
        "profile_version",
        "config_hash",
    ]


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


@pytest.mark.parametrize(
    ("overrides", "expected_constraint"),
    [
        (
            {"content_origin": None, "business_category": "99_其他内部"},
            "business_category",
        ),
        (
            {"content_origin": "external", "canonical_issuer": None},
            "canonical_issuer",
        ),
        (
            {
                "classification_status": "classified",
                "content_origin": "internal",
                "business_category": "99_其他内部",
                "canonical_issuer": "Synthetic Authority",
            },
            "canonical_issuer",
        ),
        (
            {"decision_source": "metadata_rule", "manual_locked": True, "actor": None},
            "manual_locked",
        ),
        (
            {"decision_source": "manual", "manual_locked": False, "actor": None},
            "actor",
        ),
        ({"version": 0}, "version"),
    ],
)
def test_classification_decision_rejects_invalid_routing_and_provenance(
    tmp_path: Path,
    overrides: dict[str, object],
    expected_constraint: str,
) -> None:
    database_path = tmp_path / f"invalid-{expected_constraint}.db"
    upgrade_database(database_path)
    engine = create_db_engine(database_path)
    with Session(engine) as session:
        run = models.ClassificationRun(
            run_id="invalid",
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
        )
        session.add(run)
        session.flush()
        values: dict[str, object] = {
            "classification_run_id": run.id,
            "oa_item_key": "done:invalid",
            "version": 1,
            "is_current": True,
            "decision_input_sha256": "5" * 64,
            "decision_source": "metadata_rule",
            "classification_status": "needs_review",
            "content_integrity_status": "not_checked",
            "content_origin": None,
            "initiator_type": "unknown",
            "transfer_chain_json": "[]",
            "business_category": None,
            "canonical_issuer": None,
            "normalized_title": "Synthetic invalid decision",
            "classification_confidence": 0.5,
            "classification_reason_json": "{}",
            "rule_version": "rules-v1",
            "private_config_sha256": "4" * 64,
            "manual_locked": False,
            "actor": None,
        }
        values.update(overrides)
        session.add(models.ClassificationDecision(**values))
        with pytest.raises(IntegrityError):
            session.commit()


def test_classification_evidence_sequence_must_be_positive(tmp_path: Path) -> None:
    database_path = tmp_path / "invalid-evidence-sequence.db"
    upgrade_database(database_path)
    engine = create_db_engine(database_path)
    with Session(engine) as session:
        run = models.ClassificationRun(
            run_id="evidence",
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
        )
        session.add(run)
        session.flush()
        decision = models.ClassificationDecision(
            classification_run_id=run.id,
            oa_item_key="done:evidence",
            version=1,
            is_current=True,
            decision_input_sha256="5" * 64,
            decision_source="metadata_rule",
            classification_status="needs_review",
            content_integrity_status="not_checked",
            initiator_type="unknown",
            transfer_chain_json="[]",
            normalized_title="Synthetic evidence",
            classification_confidence=0.5,
            classification_reason_json="{}",
            rule_version="rules-v1",
            private_config_sha256="4" * 64,
        )
        session.add(decision)
        session.flush()
        session.add(models.ClassificationEvidence(
            classification_decision_id=decision.id,
            sequence=0,
            evidence_type="synthetic",
            evidence_scope="package",
            value_json="{}",
            confidence=0.5,
        ))
        with pytest.raises(IntegrityError):
            session.commit()
