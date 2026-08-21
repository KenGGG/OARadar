"""Read-only, confidential inventory of locally archived Done evidence."""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from oa_knowledge.archive.integrity import sha256_file
from oa_knowledge.config import Settings
from oa_knowledge.db.models import ArchivedFile, BatchItem, OAItem, OAManifestItem
from oa_knowledge.done_archive import DONE_ARCHIVE_PREFIXES
from oa_knowledge.rebuild.paths import archive_file_relpath, resolve_rebuild_path
from oa_knowledge.storage_paths import resolve_data_path


@dataclass(frozen=True)
class InventoryRow:
    item_id: int
    file_id: int
    source_relpath: str
    destination_relpath: str
    size_bytes: int
    sha256: str
    file_role: str
    status: str


def _status_for_file(
    source: ArchivedFile,
    manifest: OAManifestItem | None,
    batch_depth_limited: bool,
    settings: Settings,
) -> str:
    if batch_depth_limited or (
        manifest is not None and manifest.processing_status == "depth_limit_reached"
    ):
        return "depth_limit_reached"
    if (
        source.download_status != "verified"
        or not source.local_relpath
        or source.size_bytes is None
        or not source.sha256
    ):
        return "missing"
    try:
        path = resolve_data_path(
            settings.data_root,
            source.local_relpath,
            allowed_prefixes=DONE_ARCHIVE_PREFIXES,
        )
    except ValueError:
        return "unsafe_path"
    if not path.is_file():
        return "missing"
    if path.stat().st_size != source.size_bytes or sha256_file(path) != source.sha256:
        return "hash_mismatch"
    return "ready"


def build_inventory(session: Session, settings: Settings) -> list[InventoryRow]:
    """Return local verification evidence without changing database or source files."""
    batch_depth_limited = exists(
        select(BatchItem.id).where(
            BatchItem.oa_item_id == OAItem.id,
            BatchItem.archive_status == "depth_limit_reached",
        )
    ).label("batch_depth_limited")
    records = session.execute(
        select(ArchivedFile, OAItem, OAManifestItem, batch_depth_limited)
        .join(OAItem, ArchivedFile.oa_item_id == OAItem.id)
        .outerjoin(OAManifestItem, OAManifestItem.oa_item_key == OAItem.oa_item_key)
        .where(OAItem.source_channel == "done")
        .order_by(OAItem.id, ArchivedFile.id)
    ).all()
    rows: list[InventoryRow] = []
    for source, item, manifest, has_batch_depth_limit in records:
        source_relpath = source.local_relpath or ""
        try:
            destination_relpath = archive_file_relpath(
                item,
                source,
                item_title_max_chars=settings.rebuild.item_title_max_chars,
            ).as_posix()
        except ValueError:
            destination_relpath = ""
            status = "unsafe_path"
        else:
            status = _status_for_file(source, manifest, has_batch_depth_limit, settings)
        rows.append(
            InventoryRow(
                item_id=item.id,
                file_id=source.id,
                source_relpath=source_relpath,
                destination_relpath=destination_relpath,
                size_bytes=source.size_bytes or 0,
                sha256=source.sha256 or "",
                file_role=source.file_role,
                status=status,
            )
        )
    return rows


def inventory_summary(rows: Sequence[InventoryRow]) -> dict[str, int]:
    """Return counts only, so callers never need to expose confidential paths."""
    summary = dict(sorted(Counter(row.status for row in rows).items()))
    summary["total"] = len(rows)
    return summary


def _private_inventory_target(settings: Settings, target: Path) -> Path:
    """Accept only a target below this settings object's private rebuild tree."""
    resolved = target.expanduser().resolve(strict=False)
    private_root = resolve_rebuild_path(settings, "state/private")
    if resolved == private_root:
        raise ValueError("private inventory target must be below state/private")
    try:
        resolved.relative_to(private_root)
    except ValueError as exc:
        raise ValueError(
            "private inventory target must be below state/private"
        ) from exc
    return resolved


def write_private_inventory(
    settings: Settings, target: Path, rows: Sequence[InventoryRow]
) -> None:
    """Write full rows only to the protected private-inventory subtree."""
    destination = _private_inventory_target(settings, target)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = (
        json.dumps(
            [asdict(row) for row in rows], ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=".inventory-", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(payload)
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()
