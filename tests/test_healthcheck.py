"""Synthetic integration test for the deployed health-check shell script."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _executable(path: Path, source: str) -> None:
    path.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
    path.chmod(0o755)


def test_healthcheck_accepts_valid_simple_status_response(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "systemctl", """
import sys
if 'list-timers' in sys.argv:
    print('synthetic timer scheduled')
""")
    _executable(bin_dir / "uv", """
import os, sys
args = sys.argv[1:]
if args[:2] == ['run', 'python']:
    os.execv(sys.executable, [sys.executable, *args[2:]])
if args[:3] == ['run', 'oa', 'schedule']:
    print('[]')
elif args[:4] == ['run', 'oa', 'knowledge', 'audit-handoff']:
    print('{"pending": 0, "failed": 0}')
elif args[:4] == ['run', 'oa', 'notifications', 'status']:
    print('{"feishu_state": "ready"}')
""")
    _executable(bin_dir / "curl", """
print('{"generated_at":"2026-01-01T00:00:00Z","overall_status":"ok",'
      '"done":{"headline":"已办正常"},"pending":{"headline":"待办正常"},'
      '"oa_activity":{},"attention":{}}')
""")
    config = tmp_path / "config.yaml"
    config.write_text(f"app:\n  data_root: {tmp_path}\nweb:\n  port: 2567\n", encoding="utf-8")
    project = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["bash", str(project / "scripts" / "healthcheck.sh")],
        cwd=project,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "OA_CONFIG": str(config)},
        capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK    simple-status" in result.stdout
    assert "WARN  simple-status" not in result.stdout
