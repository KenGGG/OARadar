from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from oa_knowledge.archive.integrity import sha256_file
from oa_knowledge.archive.manifest import ItemManifest
from oa_knowledge.archive.naming import validate_relative_path
from oa_knowledge.config import Settings


@dataclass(frozen=True)
class AuditIssue:
    code: str
    record_id: int | None
    detail: str


def audit_database(settings: Settings) -> list[AuditIssue]:
    db = settings.database_path
    if not db.exists():
        return [AuditIssue("database_missing", None, str(db))]
    try:
        connection = sqlite3.connect(db)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        return [AuditIssue("database_corrupt", None, str(exc))]
    issues: list[AuditIssue] = []
    if integrity != "ok":
        issues.append(AuditIssue("database_corrupt", None, integrity))
        connection.close()
        return issues
    for row in connection.execute(
        "SELECT id, planned_limit, discovered_count, archived_count, failed_count, skipped_count, plan_hash, status, frozen_at, "
        "(SELECT COUNT(*) FROM batch_items WHERE batch_id = collection_batches.id) FROM collection_batches"
    ):
        batch_id, limit, discovered, archived, failed, skipped, plan_hash, status, frozen_at, item_count = row
        if not 1 <= limit <= 500:
            issues.append(AuditIssue("batch_limit_invalid", batch_id, str(limit)))
        if min(discovered, archived, failed, skipped, item_count) < 0:
            issues.append(AuditIssue("batch_count_negative", batch_id, "batch counts must be non-negative"))
        if discovered != item_count:
            issues.append(AuditIssue("batch_manifest_count_mismatch", batch_id, f"discovered={discovered}, items={item_count}"))
        if archived + skipped + failed > discovered:
            issues.append(AuditIssue("batch_result_count_invalid", batch_id, "result counts exceed discovered count"))
        if len(plan_hash or "") != 64:
            issues.append(AuditIssue("batch_plan_hash_invalid", batch_id, str(plan_hash)))
        if status not in {"planned", "discovering", "ready", "running", "paused", "validating", "completed", "failed", "cancelled"}:
            issues.append(AuditIssue("batch_status_invalid", batch_id, status))
        if status not in {"planned", "cancelled"} and frozen_at is None:
            issues.append(AuditIssue("batch_not_frozen", batch_id, status))
    for file_id, relpath, expected_hash, status in connection.execute("SELECT id, local_relpath, sha256, download_status FROM files WHERE local_relpath IS NOT NULL"):
        try:
            relative = validate_relative_path(relpath)
        except ValueError as exc:
            issues.append(AuditIssue("unsafe_path", file_id, str(exc)))
            continue
        path = settings.data_root.joinpath(*relative.parts)
        if not path.exists():
            issues.append(AuditIssue("file_missing", file_id, relpath))
        elif status == "verified" and expected_hash and sha256_file(path) != expected_hash:
            issues.append(AuditIssue("hash_mismatch", file_id, relpath))
    for item_id, oa_item_id, manifest_relpath in connection.execute(
        "SELECT id, oa_item_id, archive_manifest_relpath FROM batch_items WHERE archive_status = 'archived'"
    ):
        if oa_item_id is None or not manifest_relpath:
            issues.append(AuditIssue("archive_link_missing", item_id, "archived batch item has no OA item or manifest"))
            continue
        try:
            relative = validate_relative_path(manifest_relpath)
            manifest_path = settings.data_root.joinpath(*relative.parts)
            manifest = ItemManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            issues.append(AuditIssue("manifest_invalid", item_id, str(exc)))
            continue
        manifest_files = {file.local_relpath for container in manifest.containers for file in container.files if file.local_relpath}
        database_files = {
            row[0] for row in connection.execute(
                "SELECT local_relpath FROM files WHERE oa_item_id = ? AND local_relpath IS NOT NULL", (oa_item_id,)
            )
        }
        if manifest_files != database_files:
            issues.append(AuditIssue("manifest_file_mismatch", item_id, f"manifest={len(manifest_files)}, database={len(database_files)}"))
    connection.close()
    return issues
