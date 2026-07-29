from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oa_knowledge.archive.writer import atomic_write_bytes
from oa_knowledge.config import Settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import ArchivedFile, BatchItem, CollectionBatch, ExclusionPolicy, OAItem


@dataclass(frozen=True)
class CleanupResult:
    matched_items: int
    deleted_files: int
    deleted_bytes: int
    report_relpath: str


def cleanup_excluded_archives(settings: Settings) -> CleanupResult:
    """Permanently remove local archives for active title policies, retaining list metadata."""
    engine = create_db_engine(settings.database_path)
    report_items: list[dict] = []
    deleted_files = 0
    deleted_bytes = 0
    affected_batches: set[int] = set()
    try:
        with Session(engine) as session:
            patterns = [
                policy.pattern for policy in session.scalars(
                    select(ExclusionPolicy).where(
                        ExclusionPolicy.enabled.is_(True),
                        ExclusionPolicy.scope.in_(("title", "full")),
                    )
                ).all()
            ]
            items = [
                item for item in session.scalars(select(OAItem)).all()
                if any(pattern.casefold() in item.title.casefold() for pattern in patterns)
                and item.archive_relpath
            ]
            for item in items:
                archive_dir = _safe_archive_dir(settings.data_root, item.archive_relpath)
                disk_files = sorted(path for path in archive_dir.rglob("*") if path.is_file()) if archive_dir.exists() else []
                file_records = session.scalars(select(ArchivedFile).where(ArchivedFile.oa_item_id == item.id)).all()
                report_items.append({
                    "item_id": item.id,
                    "oa_item_key": item.oa_item_key,
                    "title": item.title,
                    "matched_patterns": [p for p in patterns if p.casefold() in item.title.casefold()],
                    "archive_relpath": item.archive_relpath,
                    "files": [{
                        "relpath": str(path.relative_to(settings.data_root)),
                        "size_bytes": path.stat().st_size,
                    } for path in disk_files],
                    "database_file_records": len(file_records),
                })

            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            report_relpath = Path("state") / f"excluded-cleanup-{timestamp}.json"
            report = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "policy_patterns": patterns,
                "mode": "permanent_delete_keep_title_list",
                "items": report_items,
            }
            atomic_write_bytes(
                json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"),
                settings.data_root, report_relpath,
            )

            for item_data in report_items:
                archive_dir = _safe_archive_dir(settings.data_root, item_data["archive_relpath"])
                for path in sorted((p for p in archive_dir.rglob("*") if p.is_file()), reverse=True):
                    deleted_bytes += path.stat().st_size
                    path.unlink()
                    deleted_files += 1
                for directory in sorted((p for p in archive_dir.rglob("*") if p.is_dir()), reverse=True):
                    directory.rmdir()
                if archive_dir.exists():
                    archive_dir.rmdir()

                item = session.get(OAItem, item_data["item_id"])
                assert item is not None
                session.query(ArchivedFile).filter(ArchivedFile.oa_item_id == item.id).delete(synchronize_session=False)
                item.archive_relpath = None
                item.content_sha256 = None
                item.pipeline_status = "metadata_only"
                batch_items = session.scalars(select(BatchItem).where(BatchItem.oa_item_id == item.id)).all()
                for batch_item in batch_items:
                    affected_batches.add(batch_item.batch_id)
                    batch_item.archive_status = "confirmed_skip"
                    batch_item.skip_reason = "excluded_cleanup:downloaded_data_removed"
                    batch_item.policy_version = "web-exclusion-cleanup-v1"
                    batch_item.archive_manifest_relpath = None
                    batch_item.detail_url = None
                    batch_item.archived_at = None

            session.flush()
            for batch_id in affected_batches:
                batch = session.get(CollectionBatch, batch_id)
                if batch is None:
                    continue
                batch.archived_count = session.scalar(select(func.count()).select_from(BatchItem).where(
                    BatchItem.batch_id == batch_id, BatchItem.archive_status == "archived",
                )) or 0
                batch.skipped_count = session.scalar(select(func.count()).select_from(BatchItem).where(
                    BatchItem.batch_id == batch_id, BatchItem.archive_status == "confirmed_skip",
                )) or 0
            session.commit()
            return CleanupResult(len(report_items), deleted_files, deleted_bytes, report_relpath.as_posix())
    finally:
        engine.dispose()


def _safe_archive_dir(data_root: Path, relpath: str) -> Path:
    relative = Path(relpath)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "raw":
        raise ValueError(f"unsafe archive path: {relpath}")
    resolved = (data_root / relative).resolve()
    raw_root = (data_root / "raw").resolve()
    if resolved == raw_root or raw_root not in resolved.parents:
        raise ValueError(f"archive path escapes raw root: {relpath}")
    return resolved
