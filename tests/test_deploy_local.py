"""Tests for the unified local deployment script (plan-0806-1 §8/§9)."""

from __future__ import annotations

import subprocess
import sys
import os
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


def test_deploy_runs_tests_as_python_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = project / "config.yaml"
    config.write_text("app:\n  data_root: data\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "uv-calls.log"
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_UV_LOG\"\n"
        "if [ \"$*\" = 'run pytest -q' ]; then exit 17; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    monkeypatch.setenv("FAKE_UV_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    result = _run(["--project-root", str(project), "--config", str(config), "--skip-systemd"])

    assert result.returncode == 0, result.stderr
    assert "run python -m pytest -q" in log.read_text(encoding="utf-8").splitlines()


def test_bootstrap_finishes_before_install_enables_timers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    config = project / "config.yaml"
    config.write_text("app:\n  data_root: data\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "deploy-order.log"
    for name in ("uv", "systemctl"):
        command = bin_dir / name
        command.write_text(
            "#!/bin/sh\n"
            f"printf '{name} %s\\n' \"$*\" >> \"$FAKE_DEPLOY_LOG\"\n"
            "exit 0\n", encoding="utf-8",
        )
        command.chmod(0o755)
    installer = scripts / "install-systemd-user.sh"
    installer.write_text(
        "#!/bin/sh\nprintf 'installer\\n' >> \"$FAKE_DEPLOY_LOG\"\n",
        encoding="utf-8",
    )
    installer.chmod(0o755)
    monkeypatch.setenv("FAKE_DEPLOY_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    result = _run(["--project-root", str(project), "--config", str(config), "--bootstrap", "--skip-tests"])

    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    bootstrap = f"uv run oa schedule bootstrap --config {config}"
    assert calls.index(bootstrap) < calls.index("installer")
