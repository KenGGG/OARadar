from __future__ import annotations

import re
import unicodedata
from pathlib import Path, PurePosixPath

INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name: str, max_length: int = 160) -> str:
    value = unicodedata.normalize("NFKC", name).strip()
    value = INVALID.sub("_", value).rstrip(". ")
    value = re.sub(r"_+", "_", value)
    if value in {"", ".", ".."}:
        value = "unnamed"
    suffix = Path(value).suffix
    if len(value.encode("utf-8")) > max_length:
        suffix_bytes = len(suffix.encode("utf-8"))
        value = f"{_truncate_utf8(Path(value).stem, max(1, max_length - suffix_bytes))}{suffix}"
    return value


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")[:max_bytes]
    while encoded:
        try:
            return encoded.decode("utf-8")
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return "_"


def validate_relative_path(value: str | Path) -> PurePosixPath:
    text = str(value).replace("\\", "/")
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe relative path: {value}")
    if re.match(r"^[A-Za-z]:", text):
        raise ValueError(f"absolute Windows path is forbidden: {value}")
    return path
