"""Synthetic CLI safety checks for clean archive rebuilds."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from oa_knowledge.cli import app


def test_archive_command_defaults_to_dry_run(config_file) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["rebuild", "archive", "--config", str(config_file)])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["copied"] == 0
    assert not (config_file.parent / "data" / "state" / "oa.db").exists()
