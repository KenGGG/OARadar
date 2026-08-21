"""Synthetic CLI safety checks for clean archive rebuilds."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from oa_knowledge import cli
from oa_knowledge.cli import app
from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import (
    ArchivedFile,
    OAItem,
    OAManifestItem,
    PipelineEvent,
    PipelineRun,
    PipelineTask,
    RebuildOutput,
)
from oa_knowledge.parsers.router import ParseResult
from oa_knowledge.rebuild import cutover
from oa_knowledge.rebuild.cutover import CutoverPlan, generate_authorization_token
from oa_knowledge.rebuild.paths import resolve_rebuild_root


def _add_ready_evidence(config_file: Path) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    content = b"synthetic CLI rebuild evidence"
    relpath = "archive/raw/oa/done/synthetic/cli.bin"
    source = settings.data_root / relpath
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = OAItem(
            oa_item_key="done:synthetic-cli",
            source_channel="done",
            title="Synthetic CLI item",
            initiated_at=datetime(2026, 8, 20, tzinfo=UTC),
        )
        session.add(item)
        session.flush()
        session.add(ArchivedFile(
            oa_item_id=item.id,
            original_name="cli.bin",
            attachment_key="synthetic-cli",
            file_role="direct_attachment",
            source_container_key="root",
            local_relpath=relpath,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            download_status="verified",
        ))
        session.commit()
    engine.dispose()


def _add_build_ready_evidence(config_file: Path) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    content = b"synthetic build-ready evidence"
    relpath = "archive/raw/oa/done/synthetic/build.txt"
    source = settings.data_root / relpath
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = OAItem(
            oa_item_key="done:synthetic-build",
            source_channel="done",
            title="Synthetic build item",
            document_date=datetime(2026, 8, 20, tzinfo=UTC).date(),
            classification_state="confirmed",
            source_type="internal",
            internal_category="风险管理",
        )
        session.add(item)
        session.flush()
        session.add(ArchivedFile(
            oa_item_id=item.id,
            original_name="build.txt",
            attachment_key="synthetic-build",
            file_role="direct_attachment",
            source_container_key="root",
            local_relpath=relpath,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            download_status="verified",
        ))
        session.commit()
    engine.dispose()


def _synthetic_parse(
    source: Path, _settings, *, output_dir: Path | None = None,
) -> ParseResult:
    """Produce a parser product locally without OA, services, or network access."""
    assert output_dir is not None
    product = output_dir / "result.md"
    text = f"# synthetic\n\n{source.read_text(encoding='utf-8')}\n"
    product.write_text(text, encoding="utf-8")
    return ParseResult(product, "synthetic-local", "1", 1.0, len(text))


def test_archive_command_defaults_to_meaningful_zero_mutation_dry_run(config_file) -> None:
    _add_ready_evidence(config_file)
    settings = load_settings(config_file)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        before = {
            "runs": session.scalar(select(func.count()).select_from(PipelineRun)),
            "tasks": session.scalar(select(func.count()).select_from(PipelineTask)),
            "outputs": session.scalar(select(func.count()).select_from(RebuildOutput)),
        }
    engine.dispose()
    runner = CliRunner()
    result = runner.invoke(app, ["rebuild", "archive", "--config", str(config_file)])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {
        "blockers": {
            "depth_limit_reached": 0,
            "hash_mismatch": 0,
            "missing": 0,
            "unsafe_path": 0,
        },
        "copied": 0,
        "dry_run": True,
        "failed": 0,
        "would_copy": 1,
    }
    assert not (config_file.parent / "data_rebuilt").exists()
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        after = {
            "runs": session.scalar(select(func.count()).select_from(PipelineRun)),
            "tasks": session.scalar(select(func.count()).select_from(PipelineTask)),
            "outputs": session.scalar(select(func.count()).select_from(RebuildOutput)),
        }
    engine.dispose()
    assert after == before


def test_archive_dry_run_does_not_create_a_missing_database(config_file) -> None:
    result = CliRunner().invoke(app, [
        "rebuild", "archive", "--config", str(config_file),
    ])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"error_code": "DATABASE_NOT_INITIALIZED"}
    assert not (config_file.parent / "data").exists()


def test_archive_execute_blocks_and_persists_incomplete_inventory_counts(
    config_file,
) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = OAItem(
            oa_item_key="done:synthetic-depth-blocker",
            source_channel="done",
            title="Synthetic depth blocker",
            initiated_at=datetime(2026, 8, 20, tzinfo=UTC),
        )
        session.add(item)
        session.flush()
        session.add(OAManifestItem(
            oa_item_key=item.oa_item_key,
            title=item.title,
            list_page=0,
            processing_status="depth_limit_reached",
        ))
        session.commit()
    engine.dispose()

    result = CliRunner().invoke(app, [
        "rebuild", "archive", "--execute", "--config", str(config_file),
    ])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error_code"] == "INVENTORY_BLOCKED"
    assert payload["blockers"] == {
        "depth_limit_reached": 1,
        "hash_mismatch": 0,
        "missing": 0,
        "unsafe_path": 0,
    }
    assert payload["run_id"] > 0
    assert payload["enqueued"] == payload["copied"] == 0
    assert not (config_file.parent / "data_rebuilt").exists()

    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        run = session.get(PipelineRun, payload["run_id"])
        task = session.scalar(select(PipelineTask).where(PipelineTask.run_id == run.id))
        event = session.scalar(select(PipelineEvent).where(PipelineEvent.task_id == task.id))
        assert run.status == task.status == "failed"
        assert task.error_code == "INVENTORY_BLOCKED"
        assert json.loads(event.details_json)["depth_limit_reached"] == 1
        assert session.scalar(select(func.count()).select_from(RebuildOutput)) == 0
    engine.dispose()

    status = CliRunner().invoke(app, [
        "rebuild", "status", "--config", str(config_file),
    ])
    assert status.exit_code == 0
    status_payload = json.loads(status.stdout)
    assert status_payload["latest_run_id"] == payload["run_id"]
    assert status_payload["resumable_run_id"] == payload["run_id"]
    assert status_payload["blockers"] == payload["blockers"]


def test_successful_archive_output_includes_safe_run_id(config_file) -> None:
    _add_ready_evidence(config_file)

    result = CliRunner().invoke(app, [
        "rebuild", "archive", "--execute", "--config", str(config_file),
    ])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["run_id"] > 0
    assert payload["copied"] == 1


def test_build_command_defaults_to_a_zero_mutation_dry_run(config_file: Path) -> None:
    _add_build_ready_evidence(config_file)
    archive = CliRunner().invoke(app, [
        "rebuild", "archive", "--execute", "--config", str(config_file),
    ])
    assert archive.exit_code == 0
    run_id = json.loads(archive.stdout)["run_id"]
    settings = load_settings(config_file)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        before = {
            "runs": session.scalar(select(func.count()).select_from(PipelineRun)),
            "tasks": session.scalar(select(func.count()).select_from(PipelineTask)),
            "outputs": session.scalar(select(func.count()).select_from(RebuildOutput)),
        }
    engine.dispose()

    result = CliRunner().invoke(app, [
        "rebuild", "build", "--run-id", str(run_id), "--config", str(config_file),
    ])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "acceptance_evidence_required": True,
        "blockers": {"date_missing": 0, "needs_review": 0},
        "candidate_items": 1,
        "database_copy_ready": False,
        "dry_run": True,
        "run_id": run_id,
    }
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        after = {
            "runs": session.scalar(select(func.count()).select_from(PipelineRun)),
            "tasks": session.scalar(select(func.count()).select_from(PipelineTask)),
            "outputs": session.scalar(select(func.count()).select_from(RebuildOutput)),
        }
    engine.dispose()
    assert after == before
    assert not (resolve_rebuild_root(settings) / settings.storage.sqlite_path).exists()


def test_build_execute_requires_an_explicit_private_acceptance_attestation(
    config_file: Path,
) -> None:
    _add_build_ready_evidence(config_file)
    archive = CliRunner().invoke(app, [
        "rebuild", "archive", "--execute", "--config", str(config_file),
    ])
    assert archive.exit_code == 0
    run_id = json.loads(archive.stdout)["run_id"]

    result = CliRunner().invoke(app, [
        "rebuild", "build", "--execute", "--run-id", str(run_id),
        "--config", str(config_file),
    ])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "error_code": "BUILD_ACCEPTANCE_EVIDENCE_REQUIRED",
    }


def test_build_execute_rejects_a_group_readable_acceptance_attestation(
    config_file: Path, tmp_path: Path,
) -> None:
    _add_build_ready_evidence(config_file)
    archive = CliRunner().invoke(app, [
        "rebuild", "archive", "--execute", "--config", str(config_file),
    ])
    assert archive.exit_code == 0
    run_id = json.loads(archive.stdout)["run_id"]
    attestation = tmp_path / "unsafe-acceptance.json"
    attestation.write_text("{}", encoding="utf-8")
    attestation.chmod(0o640)

    result = CliRunner().invoke(app, [
        "rebuild", "build", "--execute", "--run-id", str(run_id),
        "--acceptance-evidence", str(attestation), "--config", str(config_file),
    ])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "error_code": "BUILD_ACCEPTANCE_EVIDENCE_INVALID",
    }


def test_build_execute_runs_rebuild_validation_and_prepares_database_copy(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_build_ready_evidence(config_file)
    archive = CliRunner().invoke(app, [
        "rebuild", "archive", "--execute", "--config", str(config_file),
    ])
    assert archive.exit_code == 0
    run_id = json.loads(archive.stdout)["run_id"]
    attestation = tmp_path / "aggregate-acceptance.json"
    attestation.write_text(json.dumps({
        "automated_tests_passed": True,
        "build_passed": True,
        "external_sample_count": 100,
        "frontend_check_passed": True,
        "internal_sample_count": 100,
        "synthetic_smoke_passed": True,
        "webui_filter_contract": True,
    }), encoding="utf-8")
    attestation.chmod(0o600)
    monkeypatch.setenv(
        "OA_REBUILD_ACCEPTANCE_EVIDENCE_HMAC_KEY",
        "synthetic-build-signing-key-material-at-least-32-bytes",
    )
    monkeypatch.setattr("oa_knowledge.rebuild.parser.parse_file", _synthetic_parse)

    result = CliRunner().invoke(app, [
        "rebuild", "build", "--execute", "--run-id", str(run_id),
        "--acceptance-evidence", str(attestation), "--config", str(config_file),
    ])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload == {
        "database_copy_ready": True,
        "dry_run": False,
        "enqueued": 1,
        "processed": 3,
        "run_id": run_id,
        "updated_files": 1,
        "validation_checks": 15,
    }
    settings = load_settings(config_file)
    copied_database = resolve_rebuild_root(settings) / settings.storage.sqlite_path
    assert copied_database.is_file()
    assert all(check.ok for check in cli.validate_database_copy(copied_database))


def test_cutover_command_defaults_to_zero_mutation_dry_run(config_file: Path) -> None:
    """Planning must not rename either directory or invoke service control."""
    settings = load_settings(config_file)
    settings.data_root.mkdir()
    rebuilt = config_file.parent / "data_rebuilt"
    rebuilt.mkdir()

    result = CliRunner().invoke(app, [
        "rebuild", "cutover", "--config", str(config_file),
    ])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["ready"] is False
    assert payload["unit_count"] == 5
    assert settings.data_root.exists() and rebuilt.exists()
    assert not list(config_file.parent.glob("data_legacy_*"))


def test_cutover_execute_requires_a_fresh_path_bound_token(
    tmp_path: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "data"
    rebuilt = tmp_path / "data_rebuilt"
    legacy = tmp_path / "data_legacy_20260822T120000Z"
    live.mkdir(); rebuilt.mkdir()
    plan = CutoverPlan(
        live_root=live, rebuilt_root=rebuilt, legacy_root=legacy,
        units=cutover.KNOWN_USER_UNITS,
        validation_ok=True, database_backup_ok=True, external_backup_ok=True, git_clean=True,
        units_discovered=True, same_filesystem=True, legacy_available=True,
    )
    monkeypatch.setenv("OA_REBUILD_CUTOVER_AUTHORIZATION_KEY", "x" * 32)
    token = generate_authorization_token(plan, now=datetime.now(UTC))
    monkeypatch.setattr(cli, "build_cutover_plan", lambda _settings, _now: plan)
    called: list[bool] = []
    monkeypatch.setattr(
        cli, "execute_cutover",
        lambda _plan, *, authorized: called.append(authorized) or {"status": "cutover_complete", "rollback": "not_required"},
    )

    denied = CliRunner().invoke(app, [
        "rebuild", "cutover", "--execute", "--config", str(config_file),
    ])
    accepted = CliRunner().invoke(app, [
        "rebuild", "cutover", "--execute", "--authorization-token", token,
        "--config", str(config_file),
    ])

    assert denied.exit_code == 1
    assert json.loads(denied.stdout) == {"error_code": "CUTOVER_AUTHORIZATION_REQUIRED"}
    assert accepted.exit_code == 0
    assert json.loads(accepted.stdout)["status"] == "cutover_complete"
    assert called == [True]


def test_cutover_dry_run_token_does_not_disclose_bound_local_paths(
    tmp_path: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "private-live"
    rebuilt = tmp_path / "private-rebuilt"
    legacy = tmp_path / "private-legacy"
    live.mkdir(); rebuilt.mkdir()
    plan = CutoverPlan(
        live_root=live, rebuilt_root=rebuilt, legacy_root=legacy,
        units=cutover.KNOWN_USER_UNITS,
        validation_ok=True, database_backup_ok=True, external_backup_ok=True, git_clean=True,
        units_discovered=True, same_filesystem=True, legacy_available=True,
    )
    monkeypatch.setenv("OA_REBUILD_CUTOVER_AUTHORIZATION_KEY", "x" * 32)
    monkeypatch.setattr(cli, "build_cutover_plan", lambda _settings, _now: plan)

    result = CliRunner().invoke(app, ["rebuild", "cutover", "--config", str(config_file)])

    assert result.exit_code == 0
    token = json.loads(result.stdout)["authorization_token"]
    assert isinstance(token, str)
    encoded = token.split(".", 1)[0]
    decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
    assert str(live) not in decoded
    assert str(rebuilt) not in decoded
    assert str(legacy) not in decoded


def test_rebuild_help_registers_all_local_commands() -> None:
    result = CliRunner().invoke(app, ["rebuild", "--help"])

    assert result.exit_code == 0
    assert all(
        f"│ {command:<10}" in result.stdout
        for command in ("inventory", "archive", "build", "status", "cutover")
    )


@pytest.mark.parametrize("command", ("inventory", "status", "archive", "build"))
def test_rebuild_commands_never_print_path_bearing_internal_errors(
    monkeypatch: pytest.MonkeyPatch, command: str, config_file
) -> None:
    def fail_settings(_config):
        raise RuntimeError("sensitive/title /private/path")

    monkeypatch.setattr(cli, "settings_option", fail_settings)
    arguments = ["rebuild", command, "--config", str(config_file)]
    if command == "archive":
        arguments.append("--execute")
    elif command == "build":
        arguments.extend(("--run-id", "1"))
    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error_code"].startswith(("REBUILD_", "BUILD_"))
    assert "sensitive" not in result.stdout
    assert "private/path" not in result.stdout


@pytest.mark.parametrize(
    ("pipeline_type", "status", "error_code"),
    (
        ("data_rebuild", "completed", "REBUILD_RUN_NOT_RESUMABLE"),
        ("other_pipeline", "running", "REBUILD_RUN_NOT_FOUND"),
    ),
)
def test_archive_execute_rejects_ineligible_explicit_resume(
    config_file, pipeline_type: str, status: str, error_code: str
) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        run = PipelineRun(
            run_key=f"synthetic-{pipeline_type}-{status}", pipeline_type=pipeline_type,
            status=status,
        )
        session.add(run)
        session.commit()
        run_id = run.id

    result = CliRunner().invoke(app, [
        "rebuild", "archive", "--execute", "--run-id", str(run_id),
        "--config", str(config_file),
    ])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"error_code": error_code}
