from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _is_ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "--", path],
        cwd=ROOT,
        check=False,
    )
    return result.returncode == 0


def test_public_private_git_boundary() -> None:
    local_only = (
        "data/example.db",
        "private/internal.md",
        "config.yaml",
        ".env",
        ".playwright-cli/page.yml",
        ".claude/settings.local.json",
        "runtime/browser-profile/Cookies",
        "webui/tsconfig.app.tsbuildinfo",
        "src/oa_knowledge/__pycache__/module.pyc",
    )
    public = (
        "config.example.yaml",
        "src/oa_knowledge/config.py",
        "tests/fixtures/login_synthetic.html",
    )

    assert {path for path in local_only if not _is_ignored(path)} == set()
    assert {path for path in public if _is_ignored(path)} == set()
