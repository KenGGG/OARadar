from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from oa_knowledge.config import Settings


def _writable_directory(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".oaradar-doctor"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def run_doctor(settings: Settings) -> list[Check]:
    root = settings.data_root
    checks = [
        Check("data_root", root.exists() and root.is_dir(), str(root)),
        Check("chrome", settings.browser.executable_path.exists(), str(settings.browser.executable_path)),
        Check("disk", shutil.disk_usage(root if root.exists() else root.parent).free > 1024**3, "at least 1 GiB free"),
        Check("gpu", shutil.which("nvidia-smi") is not None, "optional for later OCR", required=False),
        Check("archive_root", _writable_directory(settings.archive_root), str(settings.archive_root)),
        Check("markdown_root", _writable_directory(settings.markdown_root), str(settings.markdown_root)),
        Check("markdown_not_wiki", settings.markdown_root != settings.workspace_root / "wiki", str(settings.markdown_root)),
        Check("markitdown", shutil.which("markitdown") is not None, "local parser", required=False),
    ]
    try:
        from oa_knowledge.parsers.mineru_parser import mineru_available
        mineru_ok = mineru_available(settings) if settings.mineru.enabled else False
    except Exception:
        mineru_ok = False
    checks.append(Check("mineru", mineru_ok, "enabled and reachable" if settings.mineru.enabled else "disabled", required=False))
    db = settings.database_path
    if db.exists():
        try:
            with sqlite3.connect(db) as connection:
                result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            checks.append(Check("sqlite", result == "ok", result))
        except sqlite3.DatabaseError as exc:
            checks.append(Check("sqlite", False, str(exc)))
    else:
        checks.append(Check("sqlite", False, "database not initialized"))
    return checks
