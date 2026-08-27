"""Legacy binary Word conversion through a local ``wv`` installation."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from markdownify import markdownify

from oa_knowledge.config import Settings
from oa_knowledge.parsers.quality import assess_quality
from oa_knowledge.parsers.router import ParseResult


def _tool_root(settings: Settings) -> Path:
    return settings.runtime.state_root / "tools" / "wv"


def _wv_html_binary(settings: Settings) -> Path:
    system = shutil.which("wvHtml")
    if system:
        return Path(system)
    bundled = _tool_root(settings) / "usr/bin/wvHtml"
    if bundled.is_file():
        return bundled
    raise RuntimeError("wv_not_available")


def _wv_environment(settings: Settings) -> dict[str, str]:
    root = _tool_root(settings)
    env = os.environ.copy()
    bundled_bin = root / "usr/bin"
    bundled_lib = root / "usr/lib/x86_64-linux-gnu"
    if bundled_bin.is_dir():
        env["PATH"] = f"{bundled_bin}:{env.get('PATH', '')}"
    if bundled_lib.is_dir():
        existing = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{bundled_lib}:{existing}" if existing else str(bundled_lib)
    return env


def wv_engine_version(settings: Settings) -> str:
    binary = _wv_html_binary(settings)
    completed = subprocess.run(
        [str(binary), "--version"],
        env=_wv_environment(settings),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    version = (completed.stdout or completed.stderr).strip().splitlines()
    if completed.returncode != 0 or not version:
        raise RuntimeError("wv_version_unavailable")
    return version[0].strip()


def parse_with_wv(
    file_path: Path,
    settings: Settings,
    output_dir: Path | None = None,
    *,
    profile_version: str = "legacy",
) -> ParseResult:
    """Convert a legacy DOC once, preserving only a local derived Markdown file."""
    source = Path(file_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    root = output_dir or source.parent / ".parse"
    root.mkdir(parents=True, exist_ok=True)
    html_path = root / "wv-output.html"
    command = [
        str(_wv_html_binary(settings)),
        f"--datadir={_tool_root(settings) / 'usr/share'}",
        str(source),
        str(html_path),
    ]
    completed = subprocess.run(
        command,
        env=_wv_environment(settings),
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    if completed.returncode != 0 or not html_path.is_file():
        detail = (completed.stderr or completed.stdout).strip()[:300]
        raise RuntimeError(f"wv_parse_failed: {detail}" if detail else "wv_parse_failed")
    markdown_path = root / "document.md"
    markdown_path.write_text(
        markdownify(html_path.read_text(encoding="utf-8", errors="replace")),
        encoding="utf-8",
    )
    text = markdown_path.read_text(encoding="utf-8")
    quality = assess_quality(text, source)
    return ParseResult(
        output_path=markdown_path,
        engine="wv",
        engine_version=wv_engine_version(settings),
        quality_score=quality["quality_score"],
        warnings=quality["warnings"],
        text_length=quality["text_length"],
        chinese_char_ratio=quality["chinese_char_ratio"],
        replacement_char_ratio=quality["replacement_char_ratio"],
        table_count=quality["table_count"],
        image_count=quality["image_count"],
        profile_version=profile_version,
    )
