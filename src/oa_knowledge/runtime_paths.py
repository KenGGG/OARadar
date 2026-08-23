"""Safe local runtime and final-original path helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from oa_knowledge.storage_paths import resolve_data_path

if TYPE_CHECKING:
    from oa_knowledge.config import Settings


def ensure_owned_directory(path: Path, *, mode: int = 0o700) -> Path:
    """Create one private local runtime directory without following a symlink."""
    resolved = path.expanduser().resolve(strict=False)
    if resolved == resolved.parent:
        raise ValueError("runtime directory must not be a filesystem root")
    if resolved.exists() and resolved.is_symlink():
        raise ValueError("runtime directory must not be a symlink")
    resolved.mkdir(parents=True, exist_ok=True, mode=mode)
    if resolved.is_symlink() or not resolved.is_dir():
        raise ValueError("runtime directory is not a real directory")
    os.chmod(resolved, mode)
    return resolved


def resolve_original_path(settings: "Settings", relpath: str) -> Path:
    """Resolve only a final immutable-original path below ``data/originals``."""
    return resolve_data_path(
        settings.data_root,
        relpath,
        allowed_prefixes=("originals",),
    )
