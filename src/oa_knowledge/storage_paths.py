"""受保护的 ``data_root`` 相对路径解析工具。"""

from __future__ import annotations

from pathlib import Path, PurePosixPath


def _canonical_relative_path(value: str) -> PurePosixPath:
    if not value or "\\" in value:
        raise ValueError("data path must be a non-empty POSIX relative path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or value != relative.as_posix():
        raise ValueError("data path must be canonical and relative")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("data path contains an unsafe segment")
    return relative


def resolve_data_path(
    data_root: Path,
    relpath: str,
    *,
    allowed_prefixes: tuple[str, ...],
) -> Path:
    """Resolve a database path without permitting scope or symlink escapes.

    Database file paths are always POSIX paths relative to ``data_root``.  The
    returned path keeps its lexical filename (rather than following a symlink),
    after its fully resolved target has been proven to stay inside ``data_root``.
    """
    relative = _canonical_relative_path(relpath)
    prefixes = tuple(_canonical_relative_path(prefix).as_posix() for prefix in allowed_prefixes)
    if not prefixes:
        raise ValueError("at least one allowed prefix is required")
    normalized = relative.as_posix()
    if not any(normalized.startswith(f"{prefix}/") for prefix in prefixes):
        raise ValueError("data path is outside the allowed prefix")

    root = data_root.expanduser().resolve()
    candidate = root.joinpath(*relative.parts)
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("data path escapes data_root") from exc

    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("symlink data paths are not allowed")
    return candidate


def relative_data_path(data_root: Path, path: Path) -> str:
    """Return a canonical POSIX path relative to ``data_root``."""
    root = data_root.expanduser().resolve()
    resolved = path.expanduser().resolve(strict=False)
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("path is outside data_root") from exc
    if not relative.parts:
        raise ValueError("path must identify an entry below data_root")
    return relative.as_posix()
