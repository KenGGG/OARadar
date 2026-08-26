"""Read-only archive integrity assessment for OA classification."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.db.models import (
    ArchivedFile,
    ContentObject,
    OAManifestItem,
    OAItem,
    OnlineAuditItem,
)
from oa_knowledge.source_roles import AUDIT_ATTACHMENT_ROLES


ContentIntegrityStatus = Literal[
    "ok",
    "no_attachment_confirmed",
    "missing",
    "size_mismatch",
    "sha256_mismatch",
    "download_failed",
    "not_checked",
]

_SHA256_HEX = frozenset("0123456789abcdef")
_FAILED_DOWNLOAD_STATUSES = frozenset(
    {
        "failed",
        "error",
        "rejected_zero_byte",
        "rejected_error_page",
        "rejected_type_mismatch",
        "download_failed",
    }
)
_MISSING_AUDIT_STATUSES = frozenset(
    {"missing_download", "inventory_mismatch", "historical_retained", "depth_limit_reached"}
)
_REASON_ORDER = (
    "MANIFEST_MISSING",
    "ARCHIVED_ITEM_MISSING",
    "DEPTH_LIMIT_REACHED",
    "ATTACHMENT_INVENTORY_CONFLICT",
    "ZERO_ATTACHMENT_AUDIT_CONFLICT",
    "ARCHIVE_FILE_MISSING",
    "AUDIT_INVENTORY_MISMATCH",
    "DOWNLOAD_FAILED",
    "SIZE_MISMATCH",
    "SHA256_MISMATCH",
    "ZERO_ATTACHMENT_EVIDENCE_INCOMPLETE",
    "AUDIT_EVIDENCE_INVALID",
    "NOT_CHECKED",
)


@dataclass(frozen=True)
class ReadinessEvidence:
    """Normalized package-level durable evidence used by readiness and fingerprints."""

    manifest_processing_status: str | None
    manifest_no_attachment_confirmed: bool
    audit_status: str | None
    audit_finished_at: datetime | None
    audit_recognized_attachments: int | None
    audit_database_attachments: int | None
    audit_downloaded_attachments: int | None
    audit_online_inventory_sha256: str | None
    audit_local_inventory_sha256: str | None
    audit_online_content_sha256: str | None
    audit_local_content_sha256: str | None
    audit_online_evidence_sha256: str | None
    audit_local_evidence_sha256: str | None
    audit_depth_limit_reached: bool


@dataclass(frozen=True)
class IntegrityAssessment:
    """One package's independent content-integrity result."""

    content_integrity_status: ContentIntegrityStatus
    publishable: bool
    reason_codes: tuple[str, ...]
    evidence: ReadinessEvidence


def _is_sha256(value: str | None) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and set(value.lower()) <= _SHA256_HEX
    )


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _ordered_reasons(reasons: set[str]) -> tuple[str, ...]:
    known = [reason for reason in _REASON_ORDER if reason in reasons]
    return tuple((*known, *sorted(reasons.difference(_REASON_ORDER))))


def _normalized_json_sha256(raw: str | None) -> tuple[str | None, int | None]:
    if raw is None:
        return None, None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, None
    if not isinstance(value, list):
        return None, None
    try:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None, None
    return hashlib.sha256(canonical).hexdigest(), len(value)


def _evidence(manifest: OAManifestItem | None, audit: OnlineAuditItem | None) -> ReadinessEvidence:
    online_sha, _ = _normalized_json_sha256(audit.online_evidence_json if audit else None)
    local_sha, _ = _normalized_json_sha256(audit.local_evidence_json if audit else None)
    return ReadinessEvidence(
        manifest_processing_status=manifest.processing_status if manifest else None,
        manifest_no_attachment_confirmed=bool(
            manifest is not None and manifest.no_attachment_confirmed
        ),
        audit_status=audit.status if audit else None,
        audit_finished_at=_utc(audit.finished_at) if audit else None,
        audit_recognized_attachments=audit.recognized_attachments if audit else None,
        audit_database_attachments=audit.database_attachments if audit else None,
        audit_downloaded_attachments=audit.downloaded_attachments if audit else None,
        audit_online_inventory_sha256=audit.online_inventory_sha256 if audit else None,
        audit_local_inventory_sha256=audit.local_inventory_sha256 if audit else None,
        audit_online_content_sha256=audit.online_content_sha256 if audit else None,
        audit_local_content_sha256=audit.local_content_sha256 if audit else None,
        audit_online_evidence_sha256=online_sha,
        audit_local_evidence_sha256=local_sha,
        audit_depth_limit_reached=bool(audit is not None and audit.depth_limit_reached),
    )


class ArchiveReadinessService:
    """Interpret durable DB evidence without filesystem access or mutation.

    Batch consumers must call :meth:`assess_many`. The single-item method is a
    compatibility wrapper, not the execution path for historical runs.
    """

    def assess(self, session: Session, oa_item_key: str) -> IntegrityAssessment:
        key = str(oa_item_key)
        return self.assess_many(session, (key,))[key]

    def assess_many(
        self, session: Session, oa_item_keys: Sequence[str]
    ) -> dict[str, IntegrityAssessment]:
        keys = tuple(dict.fromkeys(str(key) for key in oa_item_keys))
        if not keys:
            return {}

        manifests = {
            row.oa_item_key: row
            for row in session.scalars(
                select(OAManifestItem).where(OAManifestItem.oa_item_key.in_(keys))
            )
        }
        items = {
            row.oa_item_key: row
            for row in session.scalars(
                select(OAItem).where(
                    OAItem.oa_item_key.in_(keys), OAItem.source_channel == "done"
                )
            )
        }
        item_ids = tuple(row.id for row in items.values())
        files_by_item_id: dict[int, list[ArchivedFile]] = defaultdict(list)
        if item_ids:
            for row in session.scalars(
                select(ArchivedFile).where(
                    ArchivedFile.oa_item_id.in_(item_ids),
                    ArchivedFile.file_role.in_(AUDIT_ATTACHMENT_ROLES),
                )
            ):
                files_by_item_id[row.oa_item_id].append(row)
        content_ids = {
            row.content_object_id
            for rows in files_by_item_id.values()
            for row in rows
            if row.content_object_id is not None
        }
        contents = {
            row.id: row
            for row in session.scalars(
                select(ContentObject).where(ContentObject.id.in_(content_ids))
            )
        } if content_ids else {}

        latest_audits: dict[str, OnlineAuditItem] = {}
        for row in session.scalars(
            select(OnlineAuditItem).where(
                OnlineAuditItem.oa_item_key.in_(keys),
                OnlineAuditItem.finished_at.is_not(None),
            )
        ):
            previous = latest_audits.get(row.oa_item_key)
            row_key = (_utc(row.finished_at), row.id)
            previous_key = (_utc(previous.finished_at), previous.id) if previous else None
            if previous_key is None or row_key > previous_key:
                latest_audits[row.oa_item_key] = row

        return {
            key: self._assess_loaded(
                manifests.get(key),
                items.get(key),
                files_by_item_id.get(items[key].id, ()) if key in items else (),
                contents,
                latest_audits.get(key),
            )
            for key in keys
        }

    def _assess_loaded(
        self,
        manifest: OAManifestItem | None,
        item: OAItem | None,
        files: Sequence[ArchivedFile],
        contents: dict[int, ContentObject],
        audit: OnlineAuditItem | None,
    ) -> IntegrityAssessment:
        evidence = _evidence(manifest, audit)
        reasons: set[str] = set()
        if manifest is None:
            reasons.add("MANIFEST_MISSING")
        if item is None:
            reasons.add("ARCHIVED_ITEM_MISSING")
        if item is None or manifest is None:
            return IntegrityAssessment("missing", False, _ordered_reasons(reasons), evidence)

        if manifest.processing_status == "depth_limit_reached" or (
            audit is not None and audit.depth_limit_reached
        ):
            reasons.add("DEPTH_LIMIT_REACHED")

        zero_pair = (
            manifest.no_attachment_confirmed
            and manifest.processing_status == "no_attachment"
        )
        if not files and not zero_pair:
            reasons.update({"ZERO_ATTACHMENT_EVIDENCE_INCOMPLETE", "NOT_CHECKED"})
        elif files and manifest.no_attachment_confirmed:
            reasons.add("ATTACHMENT_INVENTORY_CONFLICT")

        if manifest.processing_status in _FAILED_DOWNLOAD_STATUSES:
            reasons.add("DOWNLOAD_FAILED")
        elif manifest.processing_status not in {
            "downloaded",
            "no_attachment",
            "depth_limit_reached",
        }:
            reasons.add("NOT_CHECKED")

        for file in files:
            failed_transfer = file.download_status in _FAILED_DOWNLOAD_STATUSES
            if failed_transfer:
                reasons.add("DOWNLOAD_FAILED")
            elif file.download_status == "verified" and not file.local_relpath:
                reasons.add("ARCHIVE_FILE_MISSING")
            if (
                file.expected_size is not None
                and file.size_bytes is not None
                and file.expected_size != file.size_bytes
            ):
                reasons.add("SIZE_MISMATCH")
            content = contents.get(file.content_object_id)
            if (
                content is not None
                and content.size_bytes is not None
                and file.size_bytes is not None
                and content.size_bytes != file.size_bytes
            ):
                reasons.add("SIZE_MISMATCH")
            if (
                content is not None
                and _is_sha256(file.sha256)
                and _is_sha256(content.sha256)
                and file.sha256.lower() != content.sha256.lower()
            ):
                reasons.add("SHA256_MISMATCH")
            if content is not None and not _is_sha256(content.sha256):
                reasons.add("NOT_CHECKED")
            if (
                file.download_status != "verified"
                or file.verified_at is None
                or file.size_bytes is None
                or not _is_sha256(file.sha256)
            ):
                reasons.add("NOT_CHECKED")

        if audit is not None:
            online_sha, online_count = _normalized_json_sha256(audit.online_evidence_json)
            local_sha, local_count = _normalized_json_sha256(audit.local_evidence_json)
            if online_sha is None or local_sha is None:
                reasons.update({"AUDIT_EVIDENCE_INVALID", "NOT_CHECKED"})
            if audit.status in _MISSING_AUDIT_STATUSES:
                if audit.status == "depth_limit_reached":
                    reasons.add("DEPTH_LIMIT_REACHED")
                else:
                    reasons.add("AUDIT_INVENTORY_MISMATCH")
            elif audit.status == "content_mismatch":
                reasons.add("SHA256_MISMATCH")
            elif audit.status in {"content_unverified", "access_failed"}:
                reasons.add("NOT_CHECKED")
            elif audit.status != "matched":
                reasons.add("NOT_CHECKED")

            if zero_pair and audit.status == "matched":
                counts = (
                    audit.recognized_attachments,
                    audit.database_attachments,
                    audit.downloaded_attachments,
                    online_count,
                    local_count,
                )
                if any(count is not None and count > 0 for count in counts):
                    reasons.add("ZERO_ATTACHMENT_AUDIT_CONFLICT")
                elif audit.recognized_attachments is None:
                    reasons.update({"AUDIT_EVIDENCE_INVALID", "NOT_CHECKED"})

        ordered = _ordered_reasons(reasons)
        if reasons.intersection(
            {
                "MANIFEST_MISSING",
                "ARCHIVED_ITEM_MISSING",
                "DEPTH_LIMIT_REACHED",
                "ATTACHMENT_INVENTORY_CONFLICT",
                "ZERO_ATTACHMENT_AUDIT_CONFLICT",
                "ARCHIVE_FILE_MISSING",
                "AUDIT_INVENTORY_MISMATCH",
            }
        ):
            return IntegrityAssessment("missing", False, ordered, evidence)
        if "DOWNLOAD_FAILED" in reasons:
            return IntegrityAssessment("download_failed", False, ordered, evidence)
        if "SIZE_MISMATCH" in reasons:
            return IntegrityAssessment("size_mismatch", False, ordered, evidence)
        if "SHA256_MISMATCH" in reasons:
            return IntegrityAssessment("sha256_mismatch", False, ordered, evidence)
        if "NOT_CHECKED" in reasons:
            return IntegrityAssessment("not_checked", False, ordered, evidence)
        if not files and zero_pair:
            return IntegrityAssessment(
                "no_attachment_confirmed",
                True,
                ("NO_ATTACHMENT_CONFIRMED",),
                evidence,
            )
        return IntegrityAssessment("ok", True, ("VERIFIED_STORED_EVIDENCE",), evidence)
