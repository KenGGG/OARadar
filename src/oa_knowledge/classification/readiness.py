"""Read-only archive integrity assessment for OA classification."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
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
_MISSING_AUDIT_STATUSES = frozenset(
    {"missing_download", "inventory_mismatch", "historical_retained", "depth_limit_reached"}
)
_REASON_ORDER = (
    "MANIFEST_MISSING",
    "ARCHIVED_ITEM_MISSING",
    "DEPTH_LIMIT_REACHED",
    "ATTACHMENT_INVENTORY_CONFLICT",
    "ARCHIVE_FILE_MISSING",
    "AUDIT_INVENTORY_MISMATCH",
    "DOWNLOAD_FAILED",
    "SIZE_MISMATCH",
    "SHA256_MISMATCH",
    "NOT_CHECKED",
)


@dataclass(frozen=True)
class IntegrityAssessment:
    """One package's independent content-integrity result."""

    content_integrity_status: ContentIntegrityStatus
    publishable: bool
    reason_codes: tuple[str, ...]


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


class ArchiveReadinessService:
    """Interpret current durable manifest, file, and audit evidence.

    This service deliberately performs no filesystem access and no mutation. A
    later verified file row supersedes older audit evidence for the same item.
    """

    def assess(self, session: Session, oa_item_key: str) -> IntegrityAssessment:
        item = session.scalar(
            select(OAItem).where(
                OAItem.oa_item_key == str(oa_item_key),
                OAItem.source_channel == "done",
            )
        )
        manifest = session.scalar(
            select(OAManifestItem).where(OAManifestItem.oa_item_key == str(oa_item_key))
        )
        reasons: set[str] = set()
        if manifest is None:
            reasons.add("MANIFEST_MISSING")
        if item is None:
            reasons.add("ARCHIVED_ITEM_MISSING")
        if item is None or manifest is None:
            return IntegrityAssessment("missing", False, _ordered_reasons(reasons))

        files = list(
            session.scalars(
                select(ArchivedFile)
                .where(ArchivedFile.oa_item_id == item.id)
                .order_by(ArchivedFile.id)
            )
        )
        content_ids = {
            file.content_object_id for file in files if file.content_object_id is not None
        }
        content_evidence = {
            content.id: (content.sha256, content.size_bytes)
            for content in session.scalars(
                select(ContentObject).where(ContentObject.id.in_(content_ids))
            )
        } if content_ids else {}

        audit = session.scalar(
            select(OnlineAuditItem)
            .where(
                OnlineAuditItem.oa_item_key == str(oa_item_key),
                OnlineAuditItem.finished_at.is_not(None),
            )
            .order_by(OnlineAuditItem.finished_at.desc(), OnlineAuditItem.id.desc())
            .limit(1)
        )
        latest_file_verification = max(
            (_utc(file.verified_at) for file in files if file.verified_at is not None),
            default=None,
        )
        audit_is_current = bool(
            audit is not None
            and _utc(audit.finished_at) is not None
            and (
                latest_file_verification is None
                or _utc(audit.finished_at) >= latest_file_verification
            )
        )

        if manifest.processing_status == "depth_limit_reached" or (
            audit is not None and audit.depth_limit_reached
        ):
            reasons.add("DEPTH_LIMIT_REACHED")

        if not files:
            if not manifest.no_attachment_confirmed:
                reasons.add("NOT_CHECKED")
        elif manifest.no_attachment_confirmed:
            reasons.add("ATTACHMENT_INVENTORY_CONFLICT")

        if manifest.processing_status == "download_failed":
            reasons.add("DOWNLOAD_FAILED")
        elif manifest.processing_status not in {"downloaded", "no_attachment"}:
            reasons.add("NOT_CHECKED")

        for file in files:
            if not file.local_relpath:
                reasons.add("ARCHIVE_FILE_MISSING")
            if file.download_status == "download_failed":
                reasons.add("DOWNLOAD_FAILED")
            if (
                file.expected_size is not None
                and file.size_bytes is not None
                and file.expected_size != file.size_bytes
            ):
                reasons.add("SIZE_MISMATCH")
            content = content_evidence.get(file.content_object_id)
            content_sha = content[0] if content is not None else None
            content_size = content[1] if content is not None else None
            if (
                content_size is not None
                and file.size_bytes is not None
                and content_size != file.size_bytes
            ):
                reasons.add("SIZE_MISMATCH")
            if (
                _is_sha256(file.sha256)
                and _is_sha256(content_sha)
                and file.sha256.lower() != content_sha.lower()
            ):
                reasons.add("SHA256_MISMATCH")
            if content is not None and not _is_sha256(content_sha):
                reasons.add("NOT_CHECKED")
            if (
                file.download_status != "verified"
                or file.verified_at is None
                or file.size_bytes is None
                or not _is_sha256(file.sha256)
            ):
                reasons.add("NOT_CHECKED")

        if audit_is_current and audit is not None:
            if audit.status in _MISSING_AUDIT_STATUSES:
                if audit.status == "depth_limit_reached":
                    reasons.add("DEPTH_LIMIT_REACHED")
                else:
                    reasons.add("AUDIT_INVENTORY_MISMATCH")
            elif audit.status == "content_mismatch":
                reasons.add("SHA256_MISMATCH")
            elif audit.status in {"content_unverified", "access_failed"}:
                reasons.add("NOT_CHECKED")

        ordered = _ordered_reasons(reasons)
        if any(
            reason in reasons
            for reason in {
                "MANIFEST_MISSING",
                "ARCHIVED_ITEM_MISSING",
                "DEPTH_LIMIT_REACHED",
                "ATTACHMENT_INVENTORY_CONFLICT",
                "ARCHIVE_FILE_MISSING",
                "AUDIT_INVENTORY_MISMATCH",
            }
        ):
            return IntegrityAssessment("missing", False, ordered)
        if "DOWNLOAD_FAILED" in reasons:
            return IntegrityAssessment("download_failed", False, ordered)
        if "SIZE_MISMATCH" in reasons:
            return IntegrityAssessment("size_mismatch", False, ordered)
        if "SHA256_MISMATCH" in reasons:
            return IntegrityAssessment("sha256_mismatch", False, ordered)
        if "NOT_CHECKED" in reasons:
            return IntegrityAssessment("not_checked", False, ordered)
        if not files and manifest.no_attachment_confirmed:
            return IntegrityAssessment(
                "no_attachment_confirmed", True, ("NO_ATTACHMENT_CONFIRMED",)
            )
        return IntegrityAssessment("ok", True, ("VERIFIED_STORED_EVIDENCE",))
