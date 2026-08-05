"""Centralized archive path construction and recognition.

Every module that builds or interprets an OA raw-archive path must go through
this module so the unified ``archive/raw/oa`` prefix and the per-item
initiation-date period are applied consistently. Hard-coding ``raw/done`` or
``archive/raw/oa`` elsewhere makes the path layout drift between producers and
the reconciliation migrator (see plan-0805-02 §1.3).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath

from oa_knowledge.archive.naming import safe_filename

# Canonical unified prefix for every OA raw artifact.
ARCHIVE_PREFIX = PurePosixPath("archive", "raw", "oa")

DONE_SEGMENT = "done"
PENDING_SEGMENT = "pending"


def _period(initiated_at: datetime | None) -> str:
    return f"{initiated_at:%Y/%m}" if initiated_at else "unknown"


def done_archive_directory(
    title: str, workitem_id_text: str, initiated_at: datetime | None
) -> PurePosixPath:
    """Canonical ``archive/raw/oa/done/YYYY/MM/<title>_<workitem>`` directory."""
    return ARCHIVE_PREFIX / DONE_SEGMENT / _period(initiated_at) / f"{safe_filename(title, 100)}_{workitem_id_text}"


def pending_archive_directory(logical_item_id: int | str, snapshot_id: int | str) -> PurePosixPath:
    """Canonical ``archive/raw/oa/pending/<logical_item_id>/<snapshot_id>`` directory."""
    return ARCHIVE_PREFIX / PENDING_SEGMENT / str(logical_item_id) / str(snapshot_id)


def is_legacy_archive_path(rel: PurePosixPath) -> bool:
    """True for the old ``raw/done/...`` / ``raw/pending/...`` layout."""
    parts = rel.parts
    return len(parts) >= 2 and parts[0] == "raw" and parts[1] in (DONE_SEGMENT, PENDING_SEGMENT)


def is_current_archive_path(rel: PurePosixPath) -> bool:
    """True for the unified ``archive/raw/oa/done/...`` / ``archive/raw/oa/pending/...`` layout.

    Full-prefix validation is required: a bare ``archive/raw`` prefix is NOT
    treated as correct (plan-0805-02 §1.3).
    """
    parts = rel.parts
    return (
        len(parts) >= 4
        and parts[:3] == ARCHIVE_PREFIX.parts
        and parts[3] in (DONE_SEGMENT, PENDING_SEGMENT)
    )


def replace_archive_prefix(value: str | None, old: str, new: str) -> str | None:
    """Replace an exact path or ``old/...`` prefix inside a stored relpath string."""
    if value == old:
        return new
    if value and value.startswith(old + "/"):
        return new + value[len(old):]
    return value


def markdown_tail_from_archive_path(rel: PurePosixPath) -> PurePosixPath:
    """Return the Markdown workspace-relative tail for an archive path.

    The Markdown mirror under ``workspace/raw/sources/oa`` only carries the
    ``done/...`` or ``pending/...`` suffix, independent of whether the raw
    archive lives under ``raw/`` or the unified ``archive/raw/oa/`` prefix.
    """
    parts = rel.parts
    for index, part in enumerate(parts):
        if part in (DONE_SEGMENT, PENDING_SEGMENT) and index + 1 < len(parts):
            return PurePosixPath(*parts[index:])
    raise ValueError("archive path must contain a done/ or pending/ segment")
