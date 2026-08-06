"""Tests for the unified local deployment script (plan-0806-1 §8/§9)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy-local.sh"


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=60, check=False,
    )


def test_deploy_script_is_syntax_valid() -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_deploy_script_help_exits_zero() -> None:
    result = _run(["--help"])
    assert result.returncode == 0
    assert "Usage: deploy-local.sh" in result.stdout


def test_deploy_script_requires_args() -> None:
    result = _run([])
    assert result.returncode == 2


def test_deploy_script_rejects_unknown_arg() -> None:
    result = _run(["--nope"])
    assert result.returncode == 2


def test_deploy_script_is_executable() -> None:
    assert SCRIPT.stat().st_mode & 0o111
