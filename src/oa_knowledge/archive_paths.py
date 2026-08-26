"""Centralized archive path construction and recognition.

Done attachments live directly below ``originals/YYYY/MM/YYYY-MM-DD_title``.
The small, user-visible tree intentionally contains original attachments only;
process evidence remains in the state/cache roots.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
from pathlib import Path, PurePosixPath

from oa_knowledge.archive.naming import safe_filename

# Canonical root for immutable OA downloads.  All other acquisition evidence
# belongs in the runtime database/cache, never in ``data``.
ARCHIVE_PREFIX = PurePosixPath("originals")

DONE_SEGMENT = "done"
PENDING_SEGMENT = "pending"


def _period(initiated_at: datetime | None) -> str:
    return f"{initiated_at:%Y/%m}" if initiated_at else "unknown"


def done_archive_directory(
    title: str, workitem_id_text: str, initiated_at: datetime | None
) -> PurePosixPath:
    """Return ``originals/YYYY/MM/YYYY-MM-DD_<title>`` for a Done item.

    ``workitem_id_text`` deliberately is not exposed in the directory name:
    the original-file tree is designed for people to browse by initiation day
    and OA title.  The immutable OA identifier remains in the local database.
    """
    del workitem_id_text
    day = initiated_at.strftime("%Y-%m-%d") if initiated_at else "unknown"
    return ARCHIVE_PREFIX / _period(initiated_at) / f"{day}_{safe_filename(title, 100)}"


def done_archive_collision_directory(
    title: str, oa_item_key: str, initiated_at: datetime | None,
) -> PurePosixPath:
    """Return a stable, human-browsable collision-safe Done directory.

    The normal path intentionally remains title-based.  Only a conflicting
    existing item receives this deterministic suffix, so historic paths stay
    unchanged and every new archive remains below ``originals/``.
    """
    base = done_archive_directory(title, oa_item_key, initiated_at)
    suffix = hashlib.sha256(oa_item_key.encode("utf-8")).hexdigest()[:12]
    return base.parent / f"{base.name}__{suffix}"


def pending_archive_directory(logical_item_id: int | str, snapshot_id: int | str) -> PurePosixPath:
    """Canonical ``originals/pending/<logical_item_id>/<snapshot_id>`` directory."""
    return ARCHIVE_PREFIX / PENDING_SEGMENT / str(logical_item_id) / str(snapshot_id)


def is_legacy_archive_path(rel: PurePosixPath) -> bool:
    """True for the old ``raw/done/...`` / ``raw/pending/...`` layout."""
    parts = rel.parts
    return len(parts) >= 2 and parts[0] == "raw" and parts[1] in (DONE_SEGMENT, PENDING_SEGMENT)


def is_current_archive_path(rel: PurePosixPath) -> bool:
    """True for the current ``originals/`` archive layout."""
    parts = rel.parts
    return len(parts) >= 2 and parts[:1] == ARCHIVE_PREFIX.parts


def _original_archive_directory(data_root: Path, archive_relpath: str | None) -> Path | None:
    if not archive_relpath:
        return None
    relpath = PurePosixPath(archive_relpath)
    if not is_current_archive_path(relpath):
        return None
    data_root_resolved = data_root.resolve()
    archive_dir = (data_root / Path(*relpath.parts)).resolve()
    try:
        archive_dir.relative_to(data_root_resolved)
    except ValueError:
        return None
    if not archive_dir.is_dir():
        return None
    return archive_dir


def original_file_names(data_root: Path, archive_relpath: str | None) -> list[str]:
    """Return the actual file names a user sees in one originals directory."""
    archive_dir = _original_archive_directory(data_root, archive_relpath)
    if archive_dir is None:
        return []
    return sorted(
        (path.relative_to(archive_dir).as_posix() for path in archive_dir.rglob("*") if path.is_file()),
        key=str.casefold,
    )


def count_original_files(data_root: Path, archive_relpath: str | None) -> int:
    """Count files actually present in one current originals directory safely."""
    return len(original_file_names(data_root, archive_relpath))


def replace_archive_prefix(value: str | None, old: str, new: str) -> str | None:
    """Replace an exact path or ``old/...`` prefix inside a stored relpath string."""
    if value == old:
        return new
    if value and value.startswith(old + "/"):
        return new + value[len(old):]
    return value


def markdown_tail_from_archive_path(rel: PurePosixPath) -> PurePosixPath:
    """Return the Markdown workspace-relative tail for an archive path.

    Current archives map relative to ``originals/``. Legacy archives preserve
    their historical ``done/...`` or ``pending/...`` Markdown tail.
    """
    parts = rel.parts
    if is_current_archive_path(rel):
        return PurePosixPath(*parts[len(ARCHIVE_PREFIX.parts):])
    for index, part in enumerate(parts):
        if part in (DONE_SEGMENT, PENDING_SEGMENT) and index + 1 < len(parts):
            return PurePosixPath(*parts[index:])
    raise ValueError("archive path must be current or contain a done/ or pending/ segment")
