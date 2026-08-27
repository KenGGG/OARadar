"""只读枚举可重建数据，并生成不含 OA 业务内容的候选记录。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.models import ArchivedFile, ItemOccurrence, PipelineTask, ReviewEntry
from oa_knowledge.storage_paths import relative_data_path


SUPPORTED_CATEGORIES = frozenset({
    "browser_cache",
    "runtime_reports",
    "expired_backups",
    "sent_pending_orphans",
    "rebuildable_projection",
    "unreferenced_legacy",
})
ACTIVE_TASK_STATUSES = frozenset({"queued", "running", "retry_wait", "paused"})


@dataclass(frozen=True)
class InventoryCandidate:
    relative_path: str
    category: str
    size_bytes: int
    sha256: str
    reason_code: str


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _json_paths(raw: str | None) -> set[str]:
    if not raw:
        return set()
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return set()
    return {
        item for item in _strings(value)
        if item and not item.startswith("/") and ".." not in Path(item).parts
    }


def protected_runtime_paths(session: Session) -> set[str]:
    """Return active-task and review paths without exposing their contents."""
    protected: set[str] = set()
    tasks = session.scalars(
        select(PipelineTask).where(PipelineTask.status.in_(ACTIVE_TASK_STATUSES))
    ).all()
    for task in tasks:
        protected.update(_json_paths(task.payload_json))
    reviews = session.scalars(select(ReviewEntry).where(ReviewEntry.status == "pending")).all()
    for review in reviews:
        protected.update(_json_paths(review.details_json))
    return protected


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files(root: Path) -> Iterable[Path]:
    if not root.exists() or root.is_symlink():
        return
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            yield path


def _candidate(settings: Settings, path: Path, category: str, reason: str) -> InventoryCandidate:
    return InventoryCandidate(
        relative_path=relative_data_path(settings.data_root, path),
        category=category,
        size_bytes=path.stat().st_size,
        sha256=_sha256(path),
        reason_code=reason,
    )


def inventory_candidates(
    session: Session,
    settings: Settings,
    categories: set[str],
) -> list[InventoryCandidate]:
    unknown = categories - SUPPORTED_CATEGORIES
    if unknown:
        raise ValueError(f"unsupported cleanup categories: {','.join(sorted(unknown))}")

    protected = protected_runtime_paths(session)
    candidates: dict[str, InventoryCandidate] = {}

    def add(path: Path, category: str, reason: str) -> None:
        relpath = relative_data_path(settings.data_root, path)
        if relpath in protected:
            return
        candidates.setdefault(relpath, _candidate(settings, path, category, reason))

    # Browser/cache/runtime/projection data now lives outside ``data_root``.
    # This legacy inventory ledger stores only data-root-relative paths, so
    # these categories fail closed until a root-qualified ledger exists.

    if "expired_backups" in categories:
        for root_name in ("runtime/backups", "state/backups"):
            files = sorted(
                _files(settings.data_root / root_name),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            retained = set(files[:2])
            weekly_baselines: dict[tuple[int, int], Path] = {}
            for path in files[2:]:
                modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                iso = modified.isocalendar()
                weekly_baselines.setdefault((iso.year, iso.week), path)
            retained.update(weekly_baselines.values())
            for path in files:
                if path not in retained:
                    add(path, "expired_backups", "outside_backup_retention")

    if "sent_pending_orphans" in categories:
        has_cleaned_ledger = session.scalar(
            select(ItemOccurrence.id).where(ItemOccurrence.cleanup_status == "cleaned").limit(1)
        ) is not None
        if has_cleaned_ledger:
            referenced = set(session.scalars(
                select(ArchivedFile.local_relpath).where(ArchivedFile.local_relpath.is_not(None))
            ).all())
            for root_name in ("raw/pending", "archive/raw/oa/pending"):
                for path in _files(settings.data_root / root_name):
                    relpath = relative_data_path(settings.data_root, path)
                    if relpath not in referenced:
                        add(path, "sent_pending_orphans", "unreferenced_after_sent_cleanup")

    # ``unreferenced_legacy`` intentionally remains conservative until online
    # reconciliation can prove that every original Done item is accounted for.
    return [candidates[key] for key in sorted(candidates)]
