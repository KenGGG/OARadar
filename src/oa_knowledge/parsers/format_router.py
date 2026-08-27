"""Actual-file-type identification and parser route selection.

OA attachment display names are not reliable extensions: CAP4 can append a
human-readable size or ``_ target=``.  This module never changes a source
name; it creates an internal decision from bytes first and the cleaned display
name only as a last resort.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ActualFileType = Literal[
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "png", "jpg", "jpeg", "tif", "tiff", "bmp",
    "html", "htm", "txt", "md", "markdown", "csv", "json", "yaml", "yml", "xml",
    "zip", "rar", "7z", "ofd", "wps", "et", "ceb", "mp4", "unknown_ole", "unknown",
]

_OLE_HEADER = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_TYPE_BY_SUFFIX: dict[str, ActualFileType] = {
    "pdf": "pdf", "doc": "doc", "docx": "docx", "xls": "xls", "xlsx": "xlsx",
    "ppt": "ppt", "pptx": "pptx", "png": "png", "jpg": "jpg", "jpeg": "jpeg",
    "tif": "tif", "tiff": "tiff", "bmp": "bmp", "html": "html", "htm": "htm",
    "txt": "txt", "md": "md", "markdown": "markdown", "csv": "csv", "json": "json",
    "yaml": "yaml", "yml": "yml", "xml": "xml", "zip": "zip", "rar": "rar",
    "7z": "7z", "ofd": "ofd", "wps": "wps", "et": "et", "ceb": "ceb", "mp4": "mp4",
}
_DIRECT_TEXT = frozenset({"html", "htm", "txt", "md", "markdown", "csv", "json", "yaml", "yml", "xml"})
_VISUAL = frozenset({"pdf", "png", "jpg", "jpeg", "tif", "tiff", "bmp"})
_MARKITDOWN = frozenset({"docx", "xlsx", "ppt", "pptx"})


@dataclass(frozen=True, slots=True)
class FormatDecision:
    actual_file_type: ActualFileType
    detection_source: Literal["file_signature", "office_container", "ole_stream", "normalized_filename", "unknown"]
    filename_normalized: bool
    status_code: str = "parseable"

    @property
    def is_direct_text(self) -> bool:
        return self.actual_file_type in _DIRECT_TEXT


def _normalized_display_name(name: str) -> tuple[str, bool]:
    value = name.strip()
    cleaned = value
    # OA display decorations are metadata, not a real file suffix.
    if cleaned.lower().endswith(" target="):
        cleaned = cleaned[: -len(" target=")].rstrip()
    import re
    cleaned = re.sub(r"\s+\(\d+[mM]\)$", "", cleaned)
    return cleaned, cleaned != value


def _suffix_type(name: str) -> ActualFileType:
    suffix = Path(name).suffix.lower().lstrip(".")
    return _TYPE_BY_SUFFIX.get(suffix, "unknown")


def _ole_stream_names(path: Path) -> set[str]:
    """Read CFB stream names with olefile, returning no guess on bad containers."""
    try:
        import olefile

        with olefile.OleFileIO(path) as document:
            return {"/".join(parts) for parts in document.listdir()}
    except Exception:  # noqa: BLE001 - malformed OLE must remain unknown_ole
        return set()


def _ole_type(path: Path) -> ActualFileType:
    streams = _ole_stream_names(path)
    roots = {entry.split("/", 1)[0] for entry in streams}
    if "WordDocument" in roots:
        return "doc"
    if {"Workbook", "Book"} & roots:
        return "xls"
    if "PowerPoint Document" in roots:
        return "ppt"
    return "unknown_ole"


def _zip_office_type(path: Path) -> ActualFileType | None:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return None
    if "[Content_Types].xml" not in names:
        return None
    if any(name.startswith("word/") for name in names):
        return "docx"
    if any(name.startswith("xl/") for name in names):
        return "xlsx"
    if any(name.startswith("ppt/") for name in names):
        return "pptx"
    return None


def _status(actual_type: ActualFileType) -> str:
    if actual_type in {"rar", "7z"}:
        return "archive_container_unsupported"
    if actual_type == "zip":
        return "archive_container"
    if actual_type == "mp4":
        return "metadata_only"
    if actual_type in {"ofd", "wps", "et", "ceb", "unknown_ole", "unknown"}:
        return "unsupported_file_type"
    return "parseable"


def detect_format(path: Path) -> FormatDecision:
    """Return the actual format without mutating ``path`` or its filename."""
    source = Path(path)
    normalized_name, filename_normalized = _normalized_display_name(source.name)
    with source.open("rb") as stream:
        header = stream.read(4096)
    if header.startswith(b"%PDF-"):
        return FormatDecision("pdf", "file_signature", filename_normalized)
    if header.startswith(_OLE_HEADER):
        actual = _ole_type(source)
        return FormatDecision(actual, "ole_stream", filename_normalized, _status(actual))
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return FormatDecision("png", "file_signature", filename_normalized)
    if header.startswith(b"\xff\xd8\xff"):
        return FormatDecision("jpg", "file_signature", filename_normalized)
    if header.startswith((b"II*\x00", b"MM\x00*")):
        return FormatDecision("tiff", "file_signature", filename_normalized)
    if header.startswith(b"BM"):
        return FormatDecision("bmp", "file_signature", filename_normalized)
    office_type = _zip_office_type(source) if header.startswith(b"PK\x03\x04") else None
    if office_type is not None:
        return FormatDecision(office_type, "office_container", filename_normalized)
    actual = _suffix_type(normalized_name)
    source_kind: Literal["normalized_filename", "unknown"] = "normalized_filename" if actual != "unknown" else "unknown"
    return FormatDecision(actual, source_kind, filename_normalized, _status(actual))


def parser_attempts(decision: FormatDecision, *, mineru_enabled: bool) -> tuple[str, ...]:
    """Return at most one primary parser and one fallback parser."""
    actual = decision.actual_file_type
    if decision.status_code != "parseable" or decision.is_direct_text:
        return ()
    if actual in _VISUAL:
        return ("mineru", "markitdown") if mineru_enabled else ("markitdown",)
    if actual == "doc":
        return ("markitdown", "wv")
    if actual == "xls":
        return ("markitdown", "libreoffice")
    if actual in _MARKITDOWN:
        return ("markitdown",)
    return ()
