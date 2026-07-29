from __future__ import annotations

import importlib.util
import subprocess
import re
from pathlib import Path
from types import ModuleType

import yaml

from oa_knowledge.config import Settings, load_settings


ROOT = Path(__file__).resolve().parents[1]


def _load_release_scanner() -> ModuleType:
    scanner_path = ROOT / "scripts" / "check_public_release.py"
    assert scanner_path.exists(), "public release scanner is missing"
    spec = importlib.util.spec_from_file_location("check_public_release", scanner_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    assert "203.88.203.37" not in text  # public-release: synthetic
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


def test_release_scanner_rejects_forbidden_paths(tmp_path: Path) -> None:
    scanner = _load_release_scanner()
    paths = [
        tmp_path / "archive.db",
        tmp_path / "runtime.log",
        tmp_path / "private" / "report.md",
        tmp_path / "browser-profile" / "Cookies",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic", encoding="utf-8")

    findings = scanner.scan_paths(paths, root=tmp_path)

    assert {(finding.path, finding.rule) for finding in findings} == {
        ("archive.db", "forbidden_path"),
        ("runtime.log", "forbidden_path"),
        ("private/report.md", "forbidden_path"),
        ("browser-profile/Cookies", "forbidden_path"),
    }


def test_release_scanner_detects_sensitive_content_without_echoing_it(tmp_path: Path) -> None:
    scanner = _load_release_scanner()
    secret = "ghp_1234567890abcdefghijklmnopqrstuvwxyz"  # public-release: synthetic
    cases = {
        "endpoint.txt": "OA_URL=https://10.23.45.67/oa\n",  # public-release: synthetic
        "token.txt": f"api_key = \"{secret}\"\n",  # public-release: synthetic
        "cookie.txt": "Cookie: sessionid=synthetic-session-value\n",  # public-release: synthetic
        "path.txt": "/home/alice/private/config.yaml\n",  # public-release: synthetic
        "query.txt": "https://oa.example.invalid/list?fragmentId=1234567890123456\n",  # public-release: synthetic
    }
    paths: list[Path] = []
    for name, content in cases.items():
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        paths.append(path)

    findings = scanner.scan_paths(paths, root=tmp_path)
    rendered = "\n".join(scanner.format_finding(finding) for finding in findings)

    assert {finding.rule for finding in findings} == {
        "non_public_ip",
        "credential_value",
        "cookie_header",
        "personal_absolute_path",
        "site_numeric_identifier",
    }
    assert secret not in rendered
    assert "synthetic-session-value" not in rendered


def test_release_scanner_allows_reserved_and_loopback_examples(tmp_path: Path) -> None:
    scanner = _load_release_scanner()
    path = tmp_path / "safe.txt"
    path.write_text(
        "\n".join(
            (
                "https://oa.example.invalid/oa",
                "http://127.0.0.1:58000/health",
                "http://localhost:2567",
                "documentation address 192.0.2.10",
            )
        ),
        encoding="utf-8",
    )

    assert scanner.scan_paths([path], root=tmp_path) == []
