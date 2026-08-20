from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"app": {"data_root": str(tmp_path / "data")}}), encoding="utf-8")
    return path


def _run(args: list[str], config: Path) -> subprocess.CompletedProcess:
    command = [sys.executable, "-m", "oa_knowledge.cli", *args]
    if config is not None:
        command.extend(("--config", str(config)))
    return subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)


def test_schedule_help_returns_zero() -> None:
    result = _run(["schedule", "--help"], None)
    assert result.returncode == 0


def test_notifications_help_returns_zero() -> None:
    result = _run(["notifications", "--help"], None)
    assert result.returncode == 0


def test_default_help_hides_retired_write_command_groups() -> None:
    result = _run(["--help"], None)
    assert result.returncode == 0
    for name in ("backfill", "curate", "data", "knowledge", "wiki"):
        assert not re.search(rf"│ {re.escape(name)}\\s+", result.stdout)


def test_knowledge_help_returns_zero() -> None:
    result = _run(["knowledge", "--help"], None)
    assert result.returncode == 0


def test_schedule_subcommands_are_registered() -> None:
    result = _run(["schedule", "--help"], None)
    assert result.returncode == 0
    for name in ("bootstrap", "hourly", "nightly", "status"):
        assert name in result.stdout, f"missing schedule subcommand: {name}"


def test_notifications_subcommands_are_registered() -> None:
    result = _run(["notifications", "--help"], None)
    assert result.returncode == 0
    for name in ("test-feishu", "status", "retry"):
        assert name in result.stdout, f"missing notifications subcommand: {name}"


def test_knowledge_subcommands_are_registered() -> None:
    result = _run(["knowledge", "--help"], None)
    assert result.returncode == 0
    assert "audit-handoff" in result.stdout


def test_schedule_status_runs_on_real_database(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = _run(["schedule", "status"], config)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
