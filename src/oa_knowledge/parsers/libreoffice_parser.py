"""One-shot local LibreOffice fallback for legacy spreadsheets."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from oa_knowledge.parsers.router import ParseResult


def libreoffice_engine_version() -> str:
    executable = shutil.which("libreoffice") or shutil.which("soffice")
    if executable is None:
        return "unavailable"
    try:
        output = subprocess.run(
            [executable, "--version"], check=False, capture_output=True, text=True, timeout=10
        )
        return output.stdout.strip() or output.stderr.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def parse_with_libreoffice(
    file_path: Path,
    output_dir: Path | None = None,
    *,
    profile_version: str = "legacy",
) -> ParseResult:
    """Convert a legacy spreadsheet to a temporary XLSX then use MarkItDown once."""
    executable = shutil.which("libreoffice") or shutil.which("soffice")
    if executable is None:
        raise RuntimeError("libreoffice_unavailable")
    from oa_knowledge.parsers.markitdown_parser import parse_with_markitdown

    with tempfile.TemporaryDirectory(prefix="oaradar-xls-") as temp_name:
        temporary = Path(temp_name)
        command = [
            executable,
            "--headless",
            "--convert-to",
            "xlsx",
            "--outdir",
            str(temporary),
            str(file_path),
        ]
        completed = subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=120
        )
        converted = temporary / f"{file_path.stem}.xlsx"
        if completed.returncode != 0 or not converted.is_file():
            raise RuntimeError("libreoffice_conversion_failed")
        return parse_with_markitdown(
            converted, output_dir, profile_version=profile_version
        )
