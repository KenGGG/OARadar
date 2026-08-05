from __future__ import annotations

import os
import re
from pathlib import Path, PurePath, PureWindowsPath


def _looks_foreign_absolute(path: PurePath) -> bool:
    text = str(path)
    return bool(re.match(r"^[A-Za-z]:[\\/]", text)) or text.startswith(("\\\\", "//"))


def markdown_path_for_source(source_path: Path, raw_root: Path, markdown_root: Path) -> Path:
    """Return the sole safe mirror location for an archived source file."""
    windows_pair = isinstance(source_path, PureWindowsPath) and isinstance(raw_root, PureWindowsPath)
    for value, label in ((source_path, "source_path"), (raw_root, "raw_root"), (markdown_root, "markdown_root")):
        if ".." in value.parts or (_looks_foreign_absolute(value) and not (windows_pair and label != "markdown_root")):
            raise ValueError(f"unsafe {label}")
    if windows_pair:
        source_windows, raw_windows = PureWindowsPath(source_path), PureWindowsPath(raw_root)
        if not source_windows.is_absolute() or not raw_windows.is_absolute():
            raise ValueError("Windows paths must be absolute")
        try:
            relative = source_windows.relative_to(raw_windows, walk_up=False)
        except ValueError as exc:
            raise ValueError("source_path must be below raw_root") from exc
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("source_path has an unsafe relative path")
        return Path(markdown_root).joinpath(*relative.parts[:-1], relative.name + ".md")
    source = Path(source_path)
    raw = Path(raw_root)
    output = Path(markdown_root)
    if not source.is_absolute():
        raise ValueError("source_path must be an absolute path below raw_root")
    source = Path(os.path.abspath(source))
    raw = Path(os.path.abspath(raw))
    output = Path(os.path.abspath(output))
    try:
        relative = source.relative_to(raw)
    except ValueError as exc:
        raise ValueError("source_path must be below raw_root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("source_path has an unsafe relative path")
    target = output.joinpath(*relative.parts[:-1], relative.name + ".md")
    try:
        target.relative_to(output)
    except ValueError as exc:
        raise ValueError("Markdown output escapes markdown_root") from exc
    return target
