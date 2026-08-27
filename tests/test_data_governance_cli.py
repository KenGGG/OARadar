"""数据治理 CLI 的汇总输出测试。"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from oa_knowledge.cli import app


runner = CliRunner()


def test_data_cli_plan_and_status_are_aggregate_only(config_file: Path) -> None:
    assert runner.invoke(app, ["init", "--config", str(config_file)]).exit_code == 0
    report = config_file.parent / "data/runtime/reports/synthetic.json"
    report.parent.mkdir(parents=True)
    report.write_bytes(b"synthetic")

    planned = runner.invoke(app, [
        "data", "plan", "--categories", "runtime_reports", "--config", str(config_file),
    ])
    status = runner.invoke(app, ["data", "status", "--config", str(config_file)])

    assert planned.exit_code == 0, planned.output
    assert json.loads(planned.output)["candidate_count"] == 0
    payload = json.loads(status.output)
    assert payload["runs"][0]["candidate_count"] == 0
    assert "synthetic.json" not in status.output


def test_data_cli_commands_are_registered() -> None:
    result = runner.invoke(app, ["data", "--help"])
    assert result.exit_code == 0
    for command in ("status", "plan", "quarantine", "restore", "purge"):
        assert command in result.output
