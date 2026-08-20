"""Local-only verification and handoff facts for the Done Archive pipeline."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.archive.integrity import sha256_file
from oa_knowledge.constants import FileRole
from oa_knowledge.db.models import ArchivedFile, OAManifestItem, OAItem
from oa_knowledge.storage_paths import resolve_data_path


ATTACHMENT_ROLES = frozenset({
    str(FileRole.DIRECT_ATTACHMENT),
    str(FileRole.OFFICIAL_BODY),
    str(FileRole.OFFICIAL_ATTACHMENT),
    str(FileRole.ASSOCIATED_DOCUMENT),
    str(FileRole.OPINION_ATTACHMENT),
})
DONE_ARCHIVE_PREFIXES = ("archive/raw/oa/done", "raw/done")


@dataclass(frozen=True)
class ArchiveVerification:
    status: str
    reason: str | None = None
    content_signature: str | None = None


def verify_done_archive(session: Session, settings, oa_item_key: str) -> ArchiveVerification:
    """Prove a Done archive is locally complete without accessing OA.

    Every recorded evidence file must be verified, present below the protected
    archive root, size-matched and hash-matched.  The result intentionally has
    no dependency on ParseArtifact or Markdown state.
    """
    item = session.scalar(select(OAItem).where(
        OAItem.oa_item_key == oa_item_key,
        OAItem.source_channel == "done",
    ))
    if item is None:
        return ArchiveVerification("failed", "ARCHIVED_ITEM_MISSING")
    manifest = session.scalar(select(OAManifestItem).where(
        OAManifestItem.oa_item_key == oa_item_key,
    ))
    if manifest is None:
        return ArchiveVerification("failed", "MANIFEST_MISSING")
    if manifest.processing_status == "depth_limit_reached":
        return ArchiveVerification("failed", "DEPTH_LIMIT_REACHED")

    files = session.scalars(select(ArchivedFile).where(
        ArchivedFile.oa_item_id == item.id,
    ).order_by(ArchivedFile.id)).all()
    if not files:
        return ArchiveVerification("failed", "ARCHIVE_EVIDENCE_MISSING")
    for file in files:
        if file.download_status != "verified":
            return ArchiveVerification("failed", "ARCHIVE_FILE_UNVERIFIED")
        if not file.local_relpath or file.size_bytes is None or not file.sha256:
            return ArchiveVerification("failed", "ARCHIVE_FILE_METADATA_MISSING")
        try:
            path = resolve_data_path(
                settings.data_root, file.local_relpath,
                allowed_prefixes=DONE_ARCHIVE_PREFIXES,
            )
        except ValueError:
            return ArchiveVerification("failed", "ARCHIVE_PATH_UNSAFE")
        if not path.is_file():
            return ArchiveVerification("failed", "ARCHIVE_FILE_MISSING")
        if path.stat().st_size != file.size_bytes:
            return ArchiveVerification("failed", "ARCHIVE_SIZE_MISMATCH")
        if sha256_file(path) != file.sha256:
            return ArchiveVerification("failed", "ARCHIVE_HASH_MISMATCH")

    attachment_files = [file for file in files if file.file_role in ATTACHMENT_ROLES]
    metadata = {
        "manifest": manifest.discovery_hash,
        "item_content": item.content_sha256,
        "files": [
            [file.id, file.file_role, file.attachment_key, file.sha256]
            for file in sorted(files, key=lambda row: (row.id, row.file_role, row.attachment_key))
        ],
    }
    signature = hashlib.sha256(
        json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return ArchiveVerification("verified" if attachment_files else "no_attachment", content_signature=signature)
