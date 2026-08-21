"""Synthetic CLI safety checks for clean archive rebuilds."""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from oa_knowledge import cli
from oa_knowledge.cli import app
from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import PipelineRun


def test_archive_command_defaults_to_dry_run(config_file) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["rebuild", "archive", "--config", str(config_file)])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["copied"] == 0
    assert not (config_file.parent / "data" / "state" / "oa.db").exists()
    assert list(config_file.parent.iterdir()) == [config_file]


def test_rebuild_help_registers_all_local_commands() -> None:
    result = CliRunner().invoke(app, ["rebuild", "--help"])

    assert result.exit_code == 0
    assert all(command in result.stdout for command in ("inventory", "archive", "status"))


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
