"""Persist read-only Pending detail captures into lifecycle and source records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.archive import atomic_write_bytes, inspect_file, safe_filename
from oa_knowledge.collector.detail import DetailCapture, DirectAttachment, PageSnapshot
from oa_knowledge.constants import PipelineStatus
from oa_knowledge.db.models import (
    ArchivedFile,
    ContentObject,
    ItemOccurrence,
    OAItem,
    SourceAttachment,
)
from oa_knowledge.pending_sync import record_pending_snapshot


@dataclass(frozen=True)
class PendingCaptureResult:
    snapshot_id: int
    oa_item_id: int
    source_attachment_count: int
    verified_attachment_count: int
    failed_attachment_count: int


def _sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _capture_payload(capture: DetailCapture) -> dict:
    def snapshots(rows: tuple[PageSnapshot, ...]) -> list[dict]:
        return [
            {"name": row.name, "source_url": row.source_url, "sha256": _sha_bytes(row.html.encode("utf-8"))}
            for row in rows
        ]

    return {
        "detail_url": capture.detail_url,
        "page_family": capture.page_family,
        "body": snapshots(capture.body),
        "workflow_and_opinions": snapshots(capture.workflow),
        "attachments": [
            {
                "attachment_key": row.attachment_key,
                "filename": row.filename,
                "file_role": row.file_role,
                "mime_type": row.mime_type,
                "expected_size": row.size_bytes,
                "download_status": "verified" if row.content is not None else "download_failed",
                "sha256": _sha_bytes(row.content) if row.content is not None else None,
            }
            for row in capture.attachments
        ],
        "related_containers": [
            {
                "container_key": container.container_key,
                "parent_container_key": container.parent_container_key,
                "page_family": container.page_family,
                "depth": container.depth,
                "source_url": container.source_url,
                "snapshots": snapshots(container.snapshots),
                "attachments": [
                    {
                        "attachment_key": row.attachment_key,
                        "filename": row.filename,
                        "file_role": row.file_role,
                        "mime_type": row.mime_type,
                        "expected_size": row.size_bytes,
                        "download_status": "verified" if row.content is not None else "download_failed",
                        "sha256": _sha_bytes(row.content) if row.content is not None else None,
                    }
                    for row in container.attachments
                ],
            }
            for container in capture.related_containers
        ],
        "capture_issues": list(capture.capture_issues),
    }


def _pending_item(session: Session, occurrence: ItemOccurrence) -> OAItem:
    item = session.scalar(select(OAItem).where(OAItem.oa_item_key == occurrence.occurrence_key))
    if item is None:
        item = OAItem(
            oa_item_key=occurrence.occurrence_key,
            logical_item_id=occurrence.logical_item_id,
            workitem_id_text=occurrence.workitem_id_text,
            process_id_text=occurrence.process_id_text,
            source_channel="pending",
            title=occurrence.title or occurrence.occurrence_key,
            sender=occurrence.sender,
            received_at=occurrence.received_at,
            oa_status=occurrence.processing_status,
            pipeline_status=PipelineStatus.DISCOVERED,
        )
        session.add(item)
        session.flush()
    occurrence.oa_item_id = item.id
    item.last_seen_at = datetime.now(timezone.utc)
    return item


def _upsert_file(
    session: Session,
    item: OAItem,
    *,
    attachment_key: str,
    role: str,
    original_name: str,
    local_relpath: str | None,
    mime_type: str | None,
    size_bytes: int | None,
    sha256: str | None,
    status: str,
    source_container_key: str | None = None,
    depth: int = 1,
) -> ArchivedFile:
    row = session.scalar(select(ArchivedFile).where(
        ArchivedFile.oa_item_id == item.id,
        ArchivedFile.attachment_key == attachment_key,
        ArchivedFile.file_role == role,
    ))
    if row is None:
        row = ArchivedFile(
            oa_item_id=item.id,
            attachment_key=attachment_key,
            file_role=role,
            source_container_key=source_container_key or f"pending:{item.workitem_id_text or item.id}",
            depth=depth,
            original_name=original_name,
        )
        session.add(row)
    row.original_name = original_name
    row.local_relpath = local_relpath
    row.mime_type = mime_type
    row.size_bytes = size_bytes
    row.sha256 = sha256
    row.download_status = status
    row.download_attempts = (row.download_attempts or 0) + 1
    row.verified_at = datetime.now(timezone.utc) if status == "verified" else None
    session.flush()
    return row


def _store_snapshot_file(
    session: Session,
    item: OAItem,
    data_root: Path,
    base: PurePosixPath,
    row: PageSnapshot,
    role: str,
    index: int,
) -> ArchivedFile:
    payload = row.html.encode("utf-8")
    relpath = base / role / safe_filename(row.name)
    destination = atomic_write_bytes(payload, data_root, relpath)
    return _upsert_file(
        session, item,
        attachment_key=f"{role}:{index}", role=role, original_name=row.name,
        local_relpath=relpath.as_posix(), mime_type="application/json" if row.name.endswith(".json") else "text/html",
        size_bytes=destination.stat().st_size, sha256=_sha_bytes(payload), status="verified",
    )


def _store_attachment(
    session: Session,
    item: OAItem,
    snapshot_id: int,
    data_root: Path,
    base: PurePosixPath,
    attachment: DirectAttachment,
    ordinal: int,
    source_container_key: str | None = None,
    depth: int = 1,
) -> tuple[SourceAttachment, bool]:
    source = session.scalar(select(SourceAttachment).where(
        SourceAttachment.snapshot_id == snapshot_id,
        SourceAttachment.source_key == attachment.attachment_key,
    ))
    if source is None:
        source = SourceAttachment(
            snapshot_id=snapshot_id,
            source_key=attachment.attachment_key,
            ordinal=ordinal,
            role=attachment.file_role,
            original_name=attachment.filename,
        )
        session.add(source)

    if attachment.content is None:
        archived = _upsert_file(
            session, item, attachment_key=attachment.attachment_key, role=attachment.file_role,
            original_name=attachment.filename, local_relpath=None, mime_type=attachment.mime_type,
            size_bytes=None, sha256=None, status="download_failed",
            source_container_key=source_container_key, depth=depth,
        )
        source.source_file_id = archived.id
        source.download_status = "download_failed"
        source.retry_count = (source.retry_count or 0) + 1
        session.flush()
        return source, False

    name = safe_filename(attachment.filename)
    digest_hint = _sha_bytes(attachment.content)[:12]
    relpath = base / "attachments" / safe_filename(f"{digest_hint}_{name}")
    destination = atomic_write_bytes(attachment.content, data_root, relpath)
    expected_kind = "pdf" if destination.suffix.lower() == ".pdf" else "html_attachment" if destination.suffix.lower() in {".html", ".htm"} else None
    integrity = inspect_file(destination, expected_kind)
    archived = _upsert_file(
        session, item, attachment_key=attachment.attachment_key, role=attachment.file_role,
        original_name=attachment.filename,
        local_relpath=relpath.as_posix() if integrity.valid else None,
        mime_type=attachment.mime_type, size_bytes=integrity.size_bytes, sha256=integrity.sha256,
        status=integrity.status,
        source_container_key=source_container_key, depth=depth,
    )
    source.source_file_id = archived.id
    source.download_status = integrity.status
    source.error_code = None if integrity.valid else integrity.status
    if integrity.valid and integrity.sha256:
        content = session.scalar(select(ContentObject).where(ContentObject.sha256 == integrity.sha256))
        if content is None:
            content = ContentObject(
                sha256=integrity.sha256,
                size_bytes=integrity.size_bytes,
                detected_type=destination.suffix.lower().lstrip(".") or "unknown",
            )
            session.add(content)
            session.flush()
        archived.content_object_id = content.id
        source.content_object_id = content.id
    session.flush()
    return source, integrity.valid


def persist_pending_capture(
    session: Session,
    occurrence_key: str,
    capture: DetailCapture,
    data_root: Path,
) -> PendingCaptureResult:
    occurrence = session.scalar(select(ItemOccurrence).where(
        ItemOccurrence.occurrence_key == occurrence_key,
        ItemOccurrence.channel == "pending",
    ))
    if occurrence is None:
        raise LookupError(f"pending occurrence not found: {occurrence_key}")
    snapshot = record_pending_snapshot(session, occurrence_key, _capture_payload(capture))
    item = _pending_item(session, occurrence)
    base = PurePosixPath("raw", "pending", str(occurrence.logical_item_id), str(snapshot.id))
    for index, row in enumerate(capture.body):
        _store_snapshot_file(session, item, data_root, base, row, "body_snapshot", index)
    for index, row in enumerate(capture.workflow):
        _store_snapshot_file(session, item, data_root, base, row, "workflow_snapshot", index)
    verified = failed = 0
    attachment_rows = [(attachment, None, 1) for attachment in capture.attachments]
    for container in capture.related_containers:
        attachment_rows.extend((attachment, container.container_key, container.depth) for attachment in container.attachments)
    for ordinal, (attachment, container_key, depth) in enumerate(attachment_rows, 1):
        _, valid = _store_attachment(
            session, item, snapshot.id, data_root, base, attachment, ordinal,
            source_container_key=container_key, depth=depth,
        )
        verified += int(valid)
        failed += int(not valid)
    item.archive_relpath = base.as_posix()
    item.pipeline_status = PipelineStatus.FILES_VERIFIED if failed == 0 else PipelineStatus.DOWNLOAD_FAILED
    session.flush()
    return PendingCaptureResult(snapshot.id, item.id, len(attachment_rows), verified, failed)
