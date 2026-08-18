"""只读分类原始已办的哈希和历史清单差异。"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.archive.integrity import sha256_file
from oa_knowledge.archive.manifest import ItemManifest
from oa_knowledge.archive.naming import validate_relative_path
from oa_knowledge.archive_paths import is_current_archive_path, is_legacy_archive_path
from oa_knowledge.config import Settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import ArchivedFile, BatchItem, ContentObject, Run
from oa_knowledge.ops.audit import audit_database


@dataclass(frozen=True)
class IntegrityFinding:
    issue_code: str
    record_id: int
    reason: str


@dataclass(frozen=True)
class IntegritySummary:
    findings: tuple[IntegrityFinding, ...]
    reason_counts: dict[str, int]
    issue_counts: dict[str, int]

    @property
    def total(self) -> int:
        return len(self.findings)


def classify_hash_evidence(
    *,
    recorded_sha: str | None,
    actual_sha: str | None,
    content_object_sha: str | None,
    manifest_sha: str | None,
) -> str | None:
    if actual_sha is None:
        return "real_missing_source"
    if recorded_sha == actual_sha:
        return None
    corroborating = {value for value in (content_object_sha, manifest_sha) if value}
    if actual_sha in corroborating and recorded_sha not in corroborating:
        return "stale_recorded_hash"
    if recorded_sha in corroborating and actual_sha not in corroborating:
        return "content_changed"
    return "review_required"


def classify_manifest_evidence(
    *,
    manifest_paths: set[str],
    database_paths: set[str],
    missing_paths: set[str],
) -> str | None:
    if missing_paths:
        return "real_missing_source"
    if manifest_paths == database_paths:
        return None
    if manifest_paths < database_paths:
        return "manifest_schema_drift"
    return "review_required"


def normalize_historical_manifest_path(
    local_relpath: str,
    *,
    attachment_key: str,
    file_role: str,
    sha256: str | None,
    database_files: list[ArchivedFile],
) -> str:
    """Interpret an immutable legacy manifest after a byte-preserving move.

    A legacy path is mapped to the current DB path only when attachment identity,
    role, and a non-empty SHA-256 all agree with exactly one current file.  This
    preserves the historical manifest byte-for-byte without hiding ambiguous or
    changed evidence.
    """
    if not is_legacy_archive_path(Path(local_relpath)) or not sha256:
        return local_relpath
    matches = [
        row for row in database_files
        if row.attachment_key == attachment_key
        and row.file_role == file_role
        and row.sha256 == sha256
        and row.local_relpath
        and is_current_archive_path(Path(row.local_relpath))
    ]
    return matches[0].local_relpath if len(matches) == 1 else local_relpath


def _safe_path(settings: Settings, relative_path: str | None) -> Path | None:
    if not relative_path:
        return None
    try:
        relative = validate_relative_path(relative_path)
    except ValueError:
        return None
    return settings.data_root.joinpath(*relative.parts)


def _load_manifest(settings: Settings, relative_path: str | None) -> ItemManifest | None:
    path = _safe_path(settings, relative_path)
    if path is None:
        return None
    try:
        return ItemManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _manifest_sha_for_file(
    session: Session,
    settings: Settings,
    file: ArchivedFile,
) -> str | None:
    manifest_paths = session.scalars(select(BatchItem.archive_manifest_relpath).where(
        BatchItem.oa_item_id == file.oa_item_id,
        BatchItem.archive_manifest_relpath.is_not(None),
    )).all()
    database_files = session.scalars(select(ArchivedFile).where(
        ArchivedFile.oa_item_id == file.oa_item_id,
    )).all()
    for manifest_relpath in manifest_paths:
        manifest = _load_manifest(settings, manifest_relpath)
        if manifest is None:
            continue
        for container in manifest.containers:
            for entry in container.files:
                if not entry.local_relpath or not entry.sha256:
                    continue
                normalized = normalize_historical_manifest_path(
                    entry.local_relpath,
                    attachment_key=entry.attachment_key,
                    file_role=entry.file_role,
                    sha256=entry.sha256,
                    database_files=database_files,
                )
                if normalized == file.local_relpath:
                    return entry.sha256
    return None


def classify_integrity_issues(settings: Settings, engine=None) -> IntegritySummary:
    """Classify current integrity issues without modifying files or ledgers."""
    owned_engine = engine is None
    database_engine = engine or create_db_engine(settings.database_path)
    findings: list[IntegrityFinding] = []
    try:
        issues = audit_database(settings)
        with Session(database_engine) as session:
            for issue in issues:
                if issue.record_id is None:
                    continue
                reason: str | None = None
                if issue.code == "hash_mismatch":
                    row = session.get(ArchivedFile, issue.record_id)
                    if row is None:
                        reason = "review_required"
                    else:
                        path = _safe_path(settings, row.local_relpath)
                        actual_sha = sha256_file(path) if path is not None and path.is_file() else None
                        content_sha = session.scalar(select(ContentObject.sha256).where(
                            ContentObject.id == row.content_object_id,
                        )) if row.content_object_id is not None else None
                        manifest_sha = _manifest_sha_for_file(
                            session, settings, row,
                        )
                        reason = classify_hash_evidence(
                            recorded_sha=row.sha256,
                            actual_sha=actual_sha,
                            content_object_sha=content_sha,
                            manifest_sha=manifest_sha,
                        )
                elif issue.code == "manifest_file_mismatch":
                    item = session.get(BatchItem, issue.record_id)
                    manifest = _load_manifest(settings, item.archive_manifest_relpath) if item else None
                    if item is None or item.oa_item_id is None or manifest is None:
                        reason = "review_required"
                    else:
                        database_files = session.scalars(select(ArchivedFile).where(
                            ArchivedFile.oa_item_id == item.oa_item_id,
                        )).all()
                        manifest_paths = {
                            normalize_historical_manifest_path(
                                entry.local_relpath,
                                attachment_key=entry.attachment_key,
                                file_role=entry.file_role,
                                sha256=entry.sha256,
                                database_files=database_files,
                            )
                            for container in manifest.containers
                            for entry in container.files
                            if entry.local_relpath
                        }
                        database_paths = {
                            row.local_relpath for row in database_files if row.local_relpath
                        }
                        missing_paths = {
                            relative_path for relative_path in manifest_paths | database_paths
                            if (path := _safe_path(settings, relative_path)) is None or not path.is_file()
                        }
                        reason = classify_manifest_evidence(
                            manifest_paths=manifest_paths,
                            database_paths=database_paths,
                            missing_paths=missing_paths,
                        )
                if reason is not None:
                    findings.append(IntegrityFinding(issue.code, issue.record_id, reason))
    finally:
        if owned_engine:
            database_engine.dispose()
    return IntegritySummary(
        findings=tuple(findings),
        reason_counts=dict(Counter(finding.reason for finding in findings)),
        issue_counts=dict(Counter(finding.issue_code for finding in findings)),
    )


def persist_integrity_summary(engine, summary: IntegritySummary, *, run_key: str) -> int:
    """Persist only aggregate counts; findings, paths, titles and OA IDs stay out."""
    payload = {
        "total": summary.total,
        "issue_counts": summary.issue_counts,
        "reason_counts": summary.reason_counts,
    }
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        run = session.scalar(select(Run).where(Run.run_key == run_key))
        if run is None:
            run = Run(
                run_key=run_key,
                stage="integrity_reconciliation",
                status="completed",
                started_at=now,
            )
            session.add(run)
        run.status = "completed"
        run.finished_at = now
        run.summary_json = json.dumps(payload, sort_keys=True)
        session.commit()
        return run.id
