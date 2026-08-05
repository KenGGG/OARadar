"""Local-only, idempotent reconciliation of Done archives by initiation date."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.models import ArchivedFile, BatchItem, MarkdownExport, OAItem, OAManifestItem
from oa_knowledge.detail_archive import done_archive_directory


@dataclass(frozen=True)
class ReconciliationResult:
    item_id: int
    status: str
    old_relpath: str
    new_relpath: str
    files_updated: int = 0
    markdown_updated: int = 0


def _replace_prefix(value: str | None, old: str, new: str) -> str | None:
    if value == old:
        return new
    if value and value.startswith(old + "/"):
        return new + value[len(old):]
    return value


def _markdown_item_rel(raw_rel: Path) -> Path:
    parts = raw_rel.parts
    if not parts or parts[0] != "raw":
        raise ValueError("archive path must be below raw")
    return Path(*parts[1:])


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
    if old_rel.is_absolute() or ".." in old_rel.parts or old_rel.parts[:2] != ("raw", "done"):
        raise ValueError("unsafe archive path")
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
    if new_raw.exists() or (old_md.exists() and new_md.exists()):
        raise FileExistsError("archive target collision")

    new_raw.parent.mkdir(parents=True, exist_ok=True)
    raw_moved = md_moved = False
    try:
        old_raw.rename(new_raw)
        raw_moved = True
        if old_md.exists():
            new_md.parent.mkdir(parents=True, exist_ok=True)
            old_md.rename(new_md)
            md_moved = True
        _rewrite_text_tree(new_raw, old_rel.as_posix(), new_rel.as_posix(), old_md_rel.as_posix(), new_md_rel.as_posix())
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
