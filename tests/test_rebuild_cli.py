"""Synthetic CLI safety checks for clean archive rebuilds."""

from __future__ import annotations

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
from oa_knowledge.rebuild import cutover
from oa_knowledge.rebuild.cutover import CutoverPlan, generate_authorization_token


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


def test_rebuild_help_registers_all_local_commands() -> None:
    result = CliRunner().invoke(app, ["rebuild", "--help"])

    assert result.exit_code == 0
    assert all(command in result.stdout for command in ("inventory", "archive", "status", "cutover"))


@pytest.mark.parametrize("command", ("inventory", "status", "archive"))
def test_rebuild_commands_never_print_path_bearing_internal_errors(
    monkeypatch: pytest.MonkeyPatch, command: str, config_file
) -> None:
    def fail_settings(_config):
        raise RuntimeError("sensitive/title /private/path")

    monkeypatch.setattr(cli, "settings_option", fail_settings)
    arguments = ["rebuild", command, "--config", str(config_file)]
    if command == "archive":
        arguments.append("--execute")
    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error_code"].startswith("REBUILD_")
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
