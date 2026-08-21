"""Deterministic, protected paths for the local OA data rebuild."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from oa_knowledge.config import Settings
from oa_knowledge.db.models import ArchivedFile, OAItem
from oa_knowledge.rebuild.classification import INTERNAL_CATEGORIES

_FORBIDDEN_COMPONENT_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f-\x9f]')
_CONTROL_CHARS = re.compile(r'[\x00-\x1f\x7f-\x9f]')
_REPOSITORY_ROOT = next(
    parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").is_file()
)


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
    except ValueError:
        return False
    return True


def resolve_rebuild_root(settings: Settings) -> Path:
    """Resolve and validate the rebuild target, isolated from the live data tree."""
    configured = settings.rebuild.target_root.expanduser()
    target = configured.resolve() if configured.is_absolute() else (settings.data_root / configured).resolve()
    live_root = settings.data_root.resolve()
    home = Path.home().resolve()

    if target == Path("/") or target == home or target == _REPOSITORY_ROOT:
        raise ValueError("rebuild target must not be the filesystem root, home directory, or repository root")
    if _is_relative_to(target, live_root) or _is_relative_to(live_root, target):
        raise ValueError("rebuild target must resolve outside the live data root")
    return target


def safe_component(value: str, *, max_chars: int = 96) -> str:
    """Produce one deterministic filesystem-safe component from OA-derived text."""
    cleaned = _FORBIDDEN_COMPONENT_CHARS.sub("", value).strip().rstrip(". ")
    cleaned = cleaned[:max_chars].rstrip(". ")
    if not cleaned:
        raise ValueError("path component becomes empty after sanitization")
    return cleaned


def effective_item_date(item: OAItem) -> date:
    """Use the prescribed document, initiated, then completed date precedence."""
    for value in (item.document_date, item.initiated_at, item.completed_at):
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
    raise ValueError("item has no effective date")


def _item_folder(item: OAItem, *, item_title_max_chars: int = 96) -> str:
    item_date = effective_item_date(item)
    title = safe_component(item.title, max_chars=item_title_max_chars)
    parts = [item_date.strftime("%Y%m%d")]
    if item.document_number is not None:
        parts.append(safe_component(item.document_number))
    parts.append(title)
    short_key = hashlib.sha256(item.oa_item_key.encode("utf-8")).hexdigest()[:8]
    return "-".join(parts) + f"--{short_key}"


def archive_item_relpath(item: OAItem, *, item_title_max_chars: int = 96) -> PurePosixPath:
    """Return the classification-independent archive directory for an OA item."""
    item_date = effective_item_date(item)
    return PurePosixPath(
        "archive", "oa", "done", item_date.strftime("%Y"), item_date.strftime("%m"),
        _item_folder(item, item_title_max_chars=item_title_max_chars),
    )


def archive_file_relpath(
    item: OAItem, source: ArchivedFile, *, item_title_max_chars: int = 96,
) -> PurePosixPath:
    """Return the archive evidence path without consulting classification fields."""
    identity = f"{source.attachment_key}\0{source.file_role}\0{source.source_container_key}"
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
    return archive_item_relpath(item, item_title_max_chars=item_title_max_chars) / (
        f"{safe_component(source.original_name)}--{suffix}"
    )


def markdown_item_relpath(item: OAItem, *, item_title_max_chars: int = 96) -> PurePosixPath:
    """Return a Markdown folder only for an explicitly confirmed classification."""
    if item.classification_state != "confirmed":
        raise ValueError("Markdown paths require confirmed classification")
    item_date = effective_item_date(item)
    if item.source_type == "internal":
        if item.internal_category not in INTERNAL_CATEGORIES or item.external_issuer is not None:
            raise ValueError("confirmed internal Markdown path requires a fixed category only")
        classification_parts = ("内部事项", item.internal_category)
    elif item.source_type == "external":
        if item.internal_category is not None or not item.external_issuer:
            raise ValueError("confirmed external Markdown path requires an exact issuer only")
        classification_parts = ("外部事项", safe_component(item.external_issuer))
    else:
        raise ValueError("confirmed Markdown path requires internal or external classification")
    return PurePosixPath(
        "markdown", *classification_parts, f"{item_date:%Y}年", f"{item_date:%m}月",
        _item_folder(item, item_title_max_chars=item_title_max_chars),
    )


def resolve_rebuild_path(settings: Settings, relpath: str | PurePosixPath) -> Path:
    """Resolve a caller path strictly beneath the protected rebuild target."""
    raw = str(relpath)
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or PureWindowsPath(raw).is_absolute()
        or ".." in path.parts
        or "\\" in raw
        or _CONTROL_CHARS.search(raw)
    ):
        raise ValueError("rebuild path must be a safe relative path")
    target = resolve_rebuild_root(settings)
    resolved = (target / Path(*path.parts)).resolve()
    if resolved == target or not _is_relative_to(resolved, target):
        raise ValueError("rebuild path must be a safe relative path")
    return resolved
