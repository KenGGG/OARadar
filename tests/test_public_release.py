from __future__ import annotations

import subprocess
import re
from pathlib import Path

import yaml

from oa_knowledge.config import Settings, load_settings


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


def test_example_config_is_synthetic() -> None:
    path = ROOT / "config.example.yaml"
    text = path.read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    settings = load_settings(path)

    assert raw["browser"]["base_url"] == "https://oa.example.invalid"
    assert "203.88.203.37" not in text
    assert re.search(r"[?&][^=]+=[0-9]{12,}", text) is None
    assert raw["app"] == {
        "timezone": "Asia/Shanghai",
        "data_root": "./data",
        "privacy_mode": "local_only",
    }
    assert raw["llm"]["enabled"] is False
    assert settings.collector.max_attachment_depth == 10


def test_code_defaults_do_not_embed_a_real_oa_endpoint() -> None:
    settings = Settings()

    assert settings.browser.base_url == "https://oa.example.invalid"
    assert settings.browser.context_path == "/oa"
    assert settings.browser.login_path == "/oa/login"
    assert settings.browser.done_list_path == "/oa/done"
