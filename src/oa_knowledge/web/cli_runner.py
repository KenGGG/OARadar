"""Shared helper for invoking the ``oa_knowledge.cli`` module as a subprocess.

Both the durable worker (``web/worker.py``) and the synchronous archive-job
executor (``web/status.py``) need to run the bounded CLI runner. This module
holds the single implementation so the command assembly, timeout handling and
"last JSON line is the structured result" convention live in exactly one place.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


def run_cli(
    arguments: list[str],
    config_path: Path | None = None,
    timeout: int | None = None,
) -> tuple[int, dict]:
    """Run ``oa_knowledge.cli`` with ``arguments`` and return ``(returncode, payload)``.

    The CLI emits one JSON object per logical step on stdout; the structured
    result is the *last* line that parses as JSON. On success that is the
    machine-readable outcome (``run_status``, ``processed``, ...). A non-zero
    exit with no parseable JSON yields a payload carrying a ``reason`` and the
    ``stderr`` checksum so callers can record failure without leaking content.

    ``TimeoutExpired`` and OS-level spawn errors are converted to sentinel
    return codes (124 / 125) rather than raised, so callers can record an
    outcome uniformly.
    """
    command = [sys.executable, "-m", "oa_knowledge.cli", *arguments]
    if config_path is not None:
        command.extend(("--config", str(config_path)))
    try:
        result = subprocess.run(
            command, cwd=Path.cwd(), capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, {"reason": "timeout"}
    except OSError as exc:
        return 125, {"reason": type(exc).__name__}

    payload: dict = {}
    for line in reversed(result.stdout.splitlines()):
        try:
            payload = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if result.returncode != 0 and not payload:
        payload = {
            "reason": f"cli_exit_{result.returncode}",
            "stderr_sha256": hashlib.sha256(result.stderr.encode("utf-8", "replace")).hexdigest(),
        }
    return result.returncode, payload
