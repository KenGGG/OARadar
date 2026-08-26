"""Local-only, idempotent reconciliation of Done archives by initiation date."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import shutil

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.archive_paths import (
    done_archive_directory,
    is_current_archive_path,
    is_legacy_archive_path,
    markdown_tail_from_archive_path,
    pending_archive_directory,
    replace_archive_prefix,
)
from oa_knowledge.config import Settings
from oa_knowledge.db.models import ArchivedFile, BatchItem, MarkdownExport, OAItem, OAManifestItem

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconciliationResult:
    item_id: int
    status: str
    old_relpath: str
    new_relpath: str
    files_updated: int = 0
    markdown_updated: int = 0


def _tree_fingerprint(root: Path) -> str:
    """Hash a directory tree without following links or exposing file names.

    Archive migration is a location change, not a content migration.  Including
    relative paths and file bytes in one digest proves that the same tree arrived
    at the destination.  Links are rejected because following one could escape
    ``data_root`` and copying link text would not prove preservation of evidence.
    """
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda candidate: candidate.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError("review_required: archive contains symbolic link")
        if path.is_dir():
            digest.update(b"D\0")
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            continue
        if not path.is_file():
            raise ValueError("review_required: archive contains unsupported filesystem entry")
        digest.update(b"F\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _replace_prefix(value: str | None, old: str, new: str) -> str | None:
    return replace_archive_prefix(value, old, new)


def _markdown_item_rel(raw_rel: Path) -> Path:
    """Return the Markdown workspace-relative tail for an archive path.

    The Markdown mirror under ``workspace/raw/sources/oa`` only carries the
    ``done/...`` or ``pending/...`` suffix, independent of whether the raw
    archive lives under ``raw/`` or the unified ``archive/raw/oa/`` prefix. So a
    pure prefix migration does not move (or rewrite) any Markdown files.
    """
    return markdown_tail_from_archive_path(raw_rel)


def _rewrite_text_tree(root: Path, old_raw: str, new_raw: str, old_md: str, new_md: str) -> None:
    if not root.exists():
        return
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".json", ".md"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeError, OSError):
            continue
        updated = text.replace(old_raw, new_raw).replace(old_md, new_md)
        if updated != text:
            path.write_text(updated, encoding="utf-8")


def reconcile_item(session: Session, settings: Settings, item_id: int) -> ReconciliationResult:
    item = session.get(OAItem, item_id)
    if not item or item.source_channel != "done" or not item.archive_relpath:
        raise ValueError("done archive unavailable")
    old_rel = Path(item.archive_relpath)
    if old_rel.is_absolute() or ".." in old_rel.parts or not (
        is_legacy_archive_path(old_rel) or is_current_archive_path(old_rel)
    ):
        raise ValueError("review_required: unexpected archive prefix")
    new_rel = Path(done_archive_directory(item.title, item.workitem_id_text or str(item.id), item.initiated_at))
    if old_rel == new_rel:
        return ReconciliationResult(item.id, "already_correct", old_rel.as_posix(), new_rel.as_posix())

    old_raw = settings.data_root / old_rel
    new_raw = settings.data_root / new_rel
    old_md_rel = _markdown_item_rel(old_rel)
    new_md_rel = _markdown_item_rel(new_rel)
    old_md = settings.markdown_root / old_md_rel
    new_md = settings.markdown_root / new_md_rel
    if not old_raw.is_dir():
        raise FileNotFoundError("archive source directory missing")
    if new_raw.exists() or (old_md.exists() and new_md.exists() and old_md != new_md):
        raise FileExistsError("archive target collision")

    new_raw.parent.mkdir(parents=True, exist_ok=True)
    raw_moved = md_moved = False
    try:
        old_raw.rename(new_raw)
        raw_moved = True
        if old_md.exists() and old_md != new_md:
            new_md.parent.mkdir(parents=True, exist_ok=True)
            old_md.rename(new_md)
            md_moved = True
        _rewrite_text_tree(new_raw, old_rel.as_posix(), new_rel.as_posix(), old_md_rel.as_posix(), new_md_rel.as_posix())
        if md_moved:
            _rewrite_text_tree(new_md, old_rel.as_posix(), new_rel.as_posix(), old_md_rel.as_posix(), new_md_rel.as_posix())

        files = session.scalars(select(ArchivedFile).where(ArchivedFile.oa_item_id == item.id)).all()
        for row in files:
            row.local_relpath = _replace_prefix(row.local_relpath, old_rel.as_posix(), new_rel.as_posix())
        item.archive_relpath = new_rel.as_posix()
        manifest = session.scalar(select(OAManifestItem).where(OAManifestItem.oa_item_key == item.oa_item_key))
        if manifest:
            manifest.archive_relpath = _replace_prefix(manifest.archive_relpath, old_rel.as_posix(), new_rel.as_posix())
            manifest.initiated_at = item.initiated_at
        batches = session.scalars(select(BatchItem).where(BatchItem.oa_item_id == item.id)).all()
        for row in batches:
            row.archive_manifest_relpath = _replace_prefix(row.archive_manifest_relpath, old_rel.as_posix(), new_rel.as_posix())

        file_ids = [row.id for row in files]
        exports = session.scalars(select(MarkdownExport).where(MarkdownExport.source_file_id.in_(file_ids))).all() if file_ids else []
        old_source_prefix, new_source_prefix = old_md_rel.as_posix(), new_md_rel.as_posix()
        old_workspace_prefix = (Path("raw/sources/oa") / old_md_rel).as_posix()
        new_workspace_prefix = (Path("raw/sources/oa") / new_md_rel).as_posix()
        for row in exports:
            row.source_relpath = _replace_prefix(row.source_relpath, old_source_prefix, new_source_prefix) or row.source_relpath
            row.markdown_relpath = _replace_prefix(row.markdown_relpath, old_workspace_prefix, new_workspace_prefix) or row.markdown_relpath
            row.assets_relpath = _replace_prefix(row.assets_relpath, old_workspace_prefix, new_workspace_prefix)
            target = settings.workspace_root / row.markdown_relpath
            if target.is_file():
                row.markdown_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
        session.flush()
        return ReconciliationResult(item.id, "migrated", old_rel.as_posix(), new_rel.as_posix(), len(files), len(exports))
    except Exception:
        session.rollback()
        if md_moved and new_md.exists() and not old_md.exists():
            old_md.parent.mkdir(parents=True, exist_ok=True); new_md.rename(old_md)
        if raw_moved and new_raw.exists() and not old_raw.exists():
            old_raw.parent.mkdir(parents=True, exist_ok=True); new_raw.rename(old_raw)
        raise


def reconciliation_counts(session: Session) -> dict[str, int]:
    rows = session.scalars(select(OAItem).where(OAItem.source_channel == "done", OAItem.archive_relpath.is_not(None))).all()
    result = {"total": len(rows), "dated": 0, "unknown": 0, "correct": 0, "pending": 0}
    for item in rows:
        result["dated" if item.initiated_at else "unknown"] += 1
        expected = done_archive_directory(item.title, item.workitem_id_text or str(item.id), item.initiated_at).as_posix()
        result["correct" if item.archive_relpath == expected else "pending"] += 1
    return result


def _new_archive_relpath(item: OAItem) -> Path:
    """Compute the canonical ``originals/...`` path for a legacy item.

    Called only for items under the legacy ``raw/done`` or ``raw/pending``
    prefix (the migrator guards against other prefixes). Done items get the
    date-calibrated directory; pending items keep their logical/snapshot identity.
    """
    rel = Path(item.archive_relpath)
    parts = rel.parts
    if parts[1] == "done":
        return Path(done_archive_directory(item.title, item.workitem_id_text or str(item.id), item.initiated_at))
    # pending: raw/pending/<logical_id>/<snapshot_id> -> originals/pending/<logical_id>/<snapshot_id>
    segment_index = parts.index("pending")
    pending_parts = parts[segment_index + 1:]
    if len(pending_parts) != 2:
        raise ValueError("unexpected legacy pending path")
    return Path(pending_archive_directory(*pending_parts))


def _migrate_one_item(session: Session, settings: Settings, item: OAItem) -> str:
    """Move one archive directory from ``raw/`` to ``originals/`` and rewrite DB paths.

    Returns ``"already_correct"`` when the item is already under the unified
    prefix, or ``"migrated"`` on success. Raises on collision or missing source;
    on any failure the file move is undone and the session rolled back.
    """
    old_rel = Path(item.archive_relpath)
    if is_current_archive_path(old_rel):
        return "already_correct"
    if not is_legacy_archive_path(old_rel):
        raise ValueError("unexpected archive prefix")
    new_rel = _new_archive_relpath(item)
    if old_rel == new_rel:
        return "already_correct"

    old_raw = settings.data_root / old_rel
    new_raw = settings.data_root / new_rel
    if not old_raw.is_dir():
        raise FileNotFoundError("archive source directory missing")
    if new_raw.exists():
        raise FileExistsError("archive target collision")

    new_raw.parent.mkdir(parents=True, exist_ok=True)
    if old_raw.stat().st_dev != new_raw.parent.stat().st_dev:
        raise OSError("archive migration must stay on one filesystem")
    source_fingerprint = _tree_fingerprint(old_raw)
    raw_moved = False
    try:
        old_raw.rename(new_raw)
        raw_moved = True
        if _tree_fingerprint(new_raw) != source_fingerprint:
            raise OSError("archive fingerprint changed during migration")
        old_md_rel = _markdown_item_rel(old_rel)
        new_md_rel = _markdown_item_rel(new_rel)
        files = session.scalars(select(ArchivedFile).where(ArchivedFile.oa_item_id == item.id)).all()
        for row in files:
            row.local_relpath = _replace_prefix(row.local_relpath, old_rel.as_posix(), new_rel.as_posix())
        item.archive_relpath = new_rel.as_posix()
        manifest = session.scalar(select(OAManifestItem).where(OAManifestItem.oa_item_key == item.oa_item_key))
        if manifest:
            manifest.archive_relpath = _replace_prefix(manifest.archive_relpath, old_rel.as_posix(), new_rel.as_posix())
        batches = session.scalars(select(BatchItem).where(BatchItem.oa_item_id == item.id)).all()
        for row in batches:
            row.archive_manifest_relpath = _replace_prefix(row.archive_manifest_relpath, old_rel.as_posix(), new_rel.as_posix())
        session.flush()
        session.commit()
        return "migrated"
    except Exception:
        session.rollback()
        if raw_moved and new_raw.exists() and not old_raw.exists():
            old_raw.parent.mkdir(parents=True, exist_ok=True)
            new_raw.rename(old_raw)
        raise


def migrate_archive_item_by_id(session: Session, settings: Settings, item_id: int) -> str:
    """Migrate one local archive row and commit the filesystem/DB unit together."""
    item = session.get(OAItem, item_id)
    if item is None or item.source_channel != "done" or not item.archive_relpath:
        raise ValueError("done archive unavailable")
    return _migrate_one_item(session, settings, item)


def migrate_archive_paths(session: Session, settings: Settings, *, dry_run: bool = False) -> dict[str, int]:
    """Unify legacy ``raw/done`` and ``raw/pending`` archives under ``originals``.

    Idempotent and item-by-item safe. With ``dry_run=True`` no files or rows are
    touched; the returned counts describe what *would* change. The caller is
    expected to run this only after stopping the workers and backing up the
    database. Markdown workspace files are not moved (their relpaths are
    unaffected by the raw prefix change).
    """
    rows = session.scalars(select(OAItem).where(
        OAItem.source_channel.in_(("done", "pending")),
        OAItem.archive_relpath.is_not(None),
    )).all()
    counts: dict[str, int] = {"total": len(rows), "migrated": 0, "already_correct": 0, "skipped": 0, "failed": 0}
    for item in rows:
        rel = Path(item.archive_relpath)
        if is_current_archive_path(rel):
            counts["already_correct"] += 1
            continue
        if not is_legacy_archive_path(rel):
            counts["skipped"] += 1
            continue
        if dry_run:
            counts["migrated"] += 1
            continue
        try:
            status = _migrate_one_item(session, settings, item)
        except Exception as exc:
            counts["failed"] += 1
            logger.warning("archive path migration failed for item %s: %s", item.id, exc)
            continue
        counts[status] = counts.get(status, 0) + 1
    return counts
