from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from .naming import validate_relative_path


def atomic_commit(source: Path, root: Path, relative_target: str | Path) -> Path:
    relative = validate_relative_path(relative_target)
    destination = root.joinpath(*relative.parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".oa-", dir=destination.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        shutil.copyfile(source, temp_path)
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)
    return destination


def atomic_write_bytes(content: bytes, root: Path, relative_target: str | Path) -> Path:
    relative = validate_relative_path(relative_target)
    destination = root.joinpath(*relative.parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".oa-", dir=destination.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)
    return destination
