from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from oa_knowledge.config import Settings


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
    ]
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
