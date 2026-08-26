"""基于持久清单的同盘隔离、恢复和二次确认清除。"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from oa_knowledge.archive.writer import atomic_write_bytes
from oa_knowledge.config import Settings
from oa_knowledge.db.models import CleanupItem, CleanupRun
from oa_knowledge.storage_paths import resolve_data_path


QUARANTINE_RETENTION = timedelta(days=7)
ROOT_QUALIFIED_CATEGORIES = frozenset({
    "browser_cache",
    "runtime_reports",
    "rebuildable_projection",
})


@dataclass(frozen=True)
class CleanupExecutionSummary:
    run_id: int
    succeeded_count: int
    skipped_count: int
    failed_count: int
    processed_bytes: int


def _require_root_qualified_ledger(items: list[CleanupItem]) -> None:
    disabled = sorted({item.category for item in items} & ROOT_QUALIFIED_CATEGORIES)
    if disabled:
        raise ValueError(
            "root-qualified cleanup ledger is required for categories: "
            + ",".join(disabled)
        )


def _source_prefixes(settings: Settings, category: str) -> tuple[str, ...]:
    mapping = {
        "browser_cache": (settings.browser.user_data_dir.as_posix(),),
        "runtime_reports": ("runtime/reports",),
        "expired_backups": ("runtime/backups", "state/backups"),
        "sent_pending_orphans": ("raw/pending", "archive/raw/oa/pending"),
        "rebuildable_projection": ("parse", "vault", "workspace"),
        "unreferenced_legacy": ("raw", "archive/raw/oa"),
    }
    try:
        return mapping[category]
    except KeyError as exc:
        raise ValueError(f"unsupported cleanup category: {category}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest(settings: Settings, run: CleanupRun, items: list[CleanupItem]) -> None:
    # Deliberately omit paths: generated filenames may contain confidential OA
    # titles. The database remains the local 0600 source of path-level truth.
    payload = {
        "run_id": run.id,
        "rules_version": run.rules_version,
        "items": [
            {"item_id": item.id, "sha256": item.preflight_sha256, "status": item.status}
            for item in items
        ],
    }
    path = atomic_write_bytes(
        json.dumps(payload, sort_keys=True).encode("utf-8"),
        settings.data_root,
        f"quarantine/{run.id}/manifest.json",
    )
    path.chmod(0o600)


def quarantine_run(settings: Settings, engine: Engine, run_id: int) -> CleanupExecutionSummary:
    succeeded = skipped = failed = processed_bytes = 0
    with Session(engine) as session:
        run = session.get(CleanupRun, run_id)
        if run is None or run.status not in {"planned", "quarantined"}:
            raise ValueError("cleanup run is not ready for quarantine")
        items = session.scalars(
            select(CleanupItem).where(
                CleanupItem.cleanup_run_id == run_id,
                CleanupItem.status == "planned",
            ).order_by(CleanupItem.id)
        ).all()
        _require_root_qualified_ledger(items)
        run.status = "quarantining"
        for item in items:
            source = resolve_data_path(
                settings.data_root, item.relative_path,
                allowed_prefixes=_source_prefixes(settings, item.category),
            )
            quarantine_relpath = f"quarantine/{run_id}/{item.relative_path}"
            target = resolve_data_path(
                settings.data_root, quarantine_relpath,
                allowed_prefixes=(f"quarantine/{run_id}",),
            )
            if not source.is_file():
                item.status = "skipped"
                item.error_code = "source_missing"
                skipped += 1
                continue
            if _sha256(source) != item.preflight_sha256 or source.stat().st_size != item.size_bytes:
                item.status = "skipped"
                item.error_code = "preflight_changed"
                skipped += 1
                continue
            if target.exists():
                item.status = "failed"
                item.error_code = "quarantine_target_exists"
                failed += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
            item.status = "quarantined"
            item.quarantine_relpath = quarantine_relpath
            succeeded += 1
            processed_bytes += item.size_bytes
        run.status = "quarantined"
        run.quarantined_count += succeeded
        run.quarantined_bytes += processed_bytes
        run.finished_at = datetime.now(timezone.utc)
        _manifest(settings, run, items)
        session.commit()
    return CleanupExecutionSummary(run_id, succeeded, skipped, failed, processed_bytes)


def restore_run(settings: Settings, engine: Engine, run_id: int) -> CleanupExecutionSummary:
    succeeded = skipped = failed = processed_bytes = 0
    with Session(engine) as session:
        run = session.get(CleanupRun, run_id)
        if run is None or run.status not in {"quarantined", "restored"}:
            raise ValueError("cleanup run is not ready for restore")
        items = session.scalars(select(CleanupItem).where(
            CleanupItem.cleanup_run_id == run_id,
            CleanupItem.status == "quarantined",
        ).order_by(CleanupItem.id)).all()
        _require_root_qualified_ledger(items)
        run.status = "restoring"
        for item in items:
            source = resolve_data_path(
                settings.data_root, item.quarantine_relpath or "",
                allowed_prefixes=(f"quarantine/{run_id}",),
            )
            target = resolve_data_path(
                settings.data_root, item.relative_path,
                allowed_prefixes=_source_prefixes(settings, item.category),
            )
            if target.exists():
                item.error_code = "restore_target_exists"
                failed += 1
                continue
            if not source.is_file():
                item.error_code = "quarantine_source_missing"
                failed += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
            item.status = "restored"
            item.error_code = None
            succeeded += 1
            processed_bytes += item.size_bytes
        run.status = "restored" if failed == 0 else "quarantined"
        run.restored_count += succeeded
        run.restored_bytes += processed_bytes
        session.commit()
    return CleanupExecutionSummary(run_id, succeeded, skipped, failed, processed_bytes)


def purge_run(
    settings: Settings,
    engine: Engine,
    run_id: int,
    *,
    confirmation: str,
) -> CleanupExecutionSummary:
    if confirmation != f"PURGE-CLEANUP-RUN-{run_id}":
        raise ValueError("exact purge confirmation is required")
    succeeded = skipped = failed = processed_bytes = 0
    with Session(engine) as session:
        run = session.get(CleanupRun, run_id)
        if run is None or run.status != "quarantined" or run.finished_at is None:
            raise ValueError("cleanup run is not ready for purge")
        finished_at = run.finished_at
        if finished_at.tzinfo is None:
            finished_at = finished_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - finished_at < QUARANTINE_RETENTION:
            raise ValueError("quarantine retention period has not elapsed")
        items = session.scalars(select(CleanupItem).where(
            CleanupItem.cleanup_run_id == run_id,
            CleanupItem.status == "quarantined",
        ).order_by(CleanupItem.id)).all()
        _require_root_qualified_ledger(items)
        run.status = "purging"
        for item in items:
            path = resolve_data_path(
                settings.data_root, item.quarantine_relpath or "",
                allowed_prefixes=(f"quarantine/{run_id}",),
            )
            if path.exists():
                path.unlink()
            item.status = "purged"
            item.error_code = None
            succeeded += 1
            processed_bytes += item.size_bytes
        run.status = "purged"
        run.purged_count += succeeded
        run.purged_bytes += processed_bytes
        run.finished_at = datetime.now(timezone.utc)
        session.commit()
    return CleanupExecutionSummary(run_id, succeeded, skipped, failed, processed_bytes)
