"""Environment identity for the locally provisioned antiword fallback tool."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from oa_knowledge.config import Settings


def antiword_engine_version(settings: Settings) -> str:
    system = shutil.which("antiword")
    binary = Path(system) if system else settings.runtime.state_root / "tools/antiword/usr/bin/antiword"
    if not binary.is_file():
        raise RuntimeError("antiword_not_available")
    completed = subprocess.run(
        [str(binary), "-h"], capture_output=True, text=True, check=False, timeout=10
    )
    match = re.search(r"Version:\s*([^\r\n]+)", completed.stdout + completed.stderr)
    if match is None:
        raise RuntimeError("antiword_version_unavailable")
    return match.group(1).strip()
