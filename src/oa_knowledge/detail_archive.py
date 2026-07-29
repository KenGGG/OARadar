from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.archive import atomic_write_bytes, inspect_file, safe_filename, sha256_file
from oa_knowledge.archive.manifest import ContainerManifest, FileManifest, ItemManifest
from oa_knowledge.collector.detail import DetailCapture
from oa_knowledge.constants import FileRole, PipelineStatus
from oa_knowledge.db.models import ArchivedFile, BatchItem, OAItem, ReviewEntry


def archive_collaboration_detail(session: Session, item: BatchItem, capture: DetailCapture, data_root) -> ItemManifest:
    oa_item = session.scalar(select(OAItem).where(OAItem.oa_item_key == item.oa_item_key))
    if oa_item is None:
        oa_item = OAItem(
            oa_item_key=item.oa_item_key,
            workitem_id_text=item.workitem_id_text,
            source_channel="done",
            title=item.title,
            sender=item.sender,
            completed_at=item.completed_at,
            pipeline_status=PipelineStatus.DISCOVERED,
        )
        session.add(oa_item)
        session.flush()

    period = item.completed_at or datetime.now(timezone.utc)
    directory = PurePosixPath(
        "raw", "done", f"{period:%Y}", f"{period:%m}",
        f"{safe_filename(item.title, 100)}_{item.workitem_id_text}",
    )
    files: list[FileManifest] = []
    root_container_key = f"{capture.page_family}:{item.workitem_id_text}"
    metadata = {
        "oa_item_key": item.oa_item_key,
        "workitem_id_text": item.workitem_id_text,
        "title": item.title,
        "sender": item.sender,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
        "category": item.category,
        "detail_url": capture.detail_url,
        "page_family": capture.page_family,
        "capture_issues": list(capture.capture_issues),
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    metadata_path = _write(data_root, directory / "metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2).encode(), oa_item, FileRole.METADATA_SNAPSHOT, "metadata", root_container_key)
    files.append(metadata_path)
    for role, snapshots in ((FileRole.BODY_SNAPSHOT, capture.body), (FileRole.WORKFLOW_SNAPSHOT, capture.workflow)):
        for index, snapshot in enumerate(snapshots):
            relpath = directory / role / safe_filename(snapshot.name)
            files.append(_write(data_root, relpath, snapshot.html.encode("utf-8"), oa_item, role, f"{role}:{index}", root_container_key))
    for attachment in capture.attachments:
        files.append(_write_attachment(data_root, directory / "attachments" / _attachment_relname(attachment), oa_item, attachment, root_container_key))

    containers = [ContainerManifest(
        container_key=root_container_key,
        page_family=capture.page_family,
        depth=1,
        direct_file_count=len(files),
        files=files,
    )]
    for related in capture.related_containers:
        related_files: list[FileManifest] = []
        for index, snapshot in enumerate(related.snapshots):
            relpath = directory / "containers" / safe_filename(related.container_key) / safe_filename(snapshot.name)
            related_files.append(_write(
                data_root, relpath, snapshot.html.encode("utf-8"), oa_item,
                FileRole.BODY_SNAPSHOT, f"{related.container_key}:snapshot:{index}", related.container_key, related.depth,
            ))
        for attachment in related.attachments:
            relpath = directory / "containers" / safe_filename(related.container_key) / "files" / _attachment_relname(attachment)
            related_files.append(_write_attachment(data_root, relpath, oa_item, attachment, related.container_key, related.depth))
        containers.append(ContainerManifest(
            container_key=related.container_key,
            parent_container_key=related.parent_container_key,
            page_family=related.page_family,
            depth=related.depth,
            direct_file_count=len(related_files),
            has_unvisited_children=related.has_unvisited_children,
            files=related_files,
        ))
    for container in containers:
        container.child_container_count = sum(
            child.parent_container_key == container.container_key for child in containers
        )
    manifest = ItemManifest(
        oa_item_key=item.oa_item_key,
        workitem_id_text=item.workitem_id_text,
        title=item.title,
        captured_at=datetime.now(timezone.utc),
        containers=containers,
    )
    manifest_relpath = directory / "manifest.json"
    atomic_write_bytes(manifest.model_dump_json(indent=2).encode("utf-8"), data_root, manifest_relpath)
    oa_item.archive_relpath = directory.as_posix()
    attachment_files = [
        file for container in manifest.containers for file in container.files
        if file.file_role in {FileRole.DIRECT_ATTACHMENT, FileRole.OFFICIAL_BODY, FileRole.OFFICIAL_ATTACHMENT}
    ]
    current_attachment_keys = {file.attachment_key for file in attachment_files}
    stale_failed = session.scalars(select(ArchivedFile).where(
        ArchivedFile.oa_item_id == oa_item.id,
        ArchivedFile.file_role.in_((str(FileRole.DIRECT_ATTACHMENT), str(FileRole.OFFICIAL_BODY), str(FileRole.OFFICIAL_ATTACHMENT))),
        ArchivedFile.download_status.in_(("failed", "download_failed", "error", "rejected_error_page", "rejected_type_mismatch")),
    )).all()
    for stale in stale_failed:
        if stale.attachment_key not in current_attachment_keys:
            session.delete(stale)
    all_verified = all(file.download_status == "verified" for file in attachment_files)
    oa_item.pipeline_status = PipelineStatus.FILES_VERIFIED if all_verified and not manifest.depth_limit_reached else PipelineStatus.RAW_SAVED
    item.oa_item_id = oa_item.id
    item.detail_url = capture.detail_url
    item.archive_manifest_relpath = manifest_relpath.as_posix()
    if manifest.depth_limit_reached:
        item.archive_status = "depth_limit_reached"
    elif all_verified:
        item.archive_status = "archived"
    else:
        item.archive_status = "download_failed"
    item.archived_at = datetime.now(timezone.utc)
    item.last_error = None
    if manifest.depth_limit_reached:
        existing_review = session.scalar(select(ReviewEntry).where(
            ReviewEntry.kind == "depth_limit_reached", ReviewEntry.item_id == oa_item.id,
        ))
        if existing_review is None:
            session.add(ReviewEntry(
                kind="depth_limit_reached", item_id=oa_item.id,
                container_key=next(container.container_key for container in manifest.containers if container.has_unvisited_children),
                depth=10, details_json=json.dumps({"workitem_id_text": item.workitem_id_text}),
            ))
    for issue in capture.capture_issues:
        container_key = str(issue.get("container_key") or root_container_key)
        existing_review = session.scalar(select(ReviewEntry).where(
            ReviewEntry.kind == "associated_container_unavailable",
            ReviewEntry.item_id == oa_item.id,
            ReviewEntry.container_key == container_key,
            ReviewEntry.status == "pending",
        ))
        if existing_review is None:
            session.add(ReviewEntry(
                kind="associated_container_unavailable",
                item_id=oa_item.id,
                container_key=container_key,
                depth=int(issue.get("depth") or 1),
                details_json=json.dumps(issue, ensure_ascii=False),
            ))
    session.flush()
    return manifest


def _write(data_root, relpath: PurePosixPath, content: bytes, oa_item: OAItem, role: FileRole, key: str, container_key: str | None = None, depth: int = 1) -> FileManifest:
    destination = atomic_write_bytes(content, data_root, relpath)
    digest = sha256_file(destination)
    session = Session.object_session(oa_item)
    assert session is not None
    archived = session.scalar(select(ArchivedFile).where(
        ArchivedFile.oa_item_id == oa_item.id,
        ArchivedFile.attachment_key == key,
        ArchivedFile.file_role == str(role),
    )) or ArchivedFile(oa_item_id=oa_item.id, attachment_key=key, file_role=str(role), original_name=relpath.name, source_container_key=container_key or f"collaboration:{oa_item.workitem_id_text}", depth=1)
    archived.original_name = relpath.name
    archived.local_relpath = relpath.as_posix()
    archived.mime_type = "application/json" if relpath.suffix == ".json" else "text/html"
    archived.size_bytes = destination.stat().st_size
    archived.sha256 = digest
    archived.source_container_key = container_key or f"collaboration:{oa_item.workitem_id_text}"
    archived.depth = depth
    archived.download_status = "verified"
    archived.verified_at = datetime.now(timezone.utc)
    session.add(archived)
    return FileManifest(
        attachment_key=key,
        original_name=relpath.name,
        local_relpath=relpath.as_posix(),
        file_role=role,
        source_container_key=archived.source_container_key,
        size_bytes=archived.size_bytes,
        sha256=digest,
        download_status="verified",
    )


def _write_attachment(data_root, relpath: PurePosixPath, oa_item: OAItem, attachment, container_key: str, depth: int = 1) -> FileManifest:
    session = Session.object_session(oa_item)
    assert session is not None
    role = FileRole(attachment.file_role)
    archived = session.scalar(select(ArchivedFile).where(
        ArchivedFile.oa_item_id == oa_item.id,
        ArchivedFile.attachment_key == attachment.attachment_key,
        ArchivedFile.file_role == str(role),
    ))
    if archived is None:
        # CAP4 exposes a batch URL on one run and an individual download
        # control on the next. Reconcile an old failed row by filename so a
        # successful retry does not inflate attachment totals.
        compatible_roles = (str(role),)
        if role in {FileRole.DIRECT_ATTACHMENT, FileRole.OFFICIAL_ATTACHMENT}:
            compatible_roles = (str(FileRole.DIRECT_ATTACHMENT), str(FileRole.OFFICIAL_ATTACHMENT))
        archived = session.scalar(select(ArchivedFile).where(
            ArchivedFile.oa_item_id == oa_item.id,
            ArchivedFile.file_role.in_(compatible_roles),
            ArchivedFile.original_name == attachment.filename,
            ArchivedFile.download_status.in_(("failed", "download_failed", "error", "rejected_error_page")),
        ).order_by(ArchivedFile.id).limit(1))
    if archived is None:
        archived = ArchivedFile(
        oa_item_id=oa_item.id,
        original_name=relpath.name,
        attachment_key=attachment.attachment_key,
        file_role=str(role),
        source_container_key=container_key,
        depth=1,
        )
    archived.attachment_key = attachment.attachment_key
    archived.file_role = str(role)
    archived.original_name = attachment.filename
    archived.mime_type = attachment.mime_type
    archived.expected_size = attachment.size_bytes
    archived.depth = depth
    archived.download_attempts = (archived.download_attempts or 0) + 1
    if attachment.content is None:
        archived.download_status = "download_failed"
        session.add(archived)
        return FileManifest(
            attachment_key=attachment.attachment_key, original_name=attachment.filename,
            file_role=role, source_container_key=container_key, download_status="download_failed",
        )
    destination = atomic_write_bytes(attachment.content, data_root, relpath)
    expected_kind = "pdf" if relpath.suffix.lower() == ".pdf" else "html_attachment" if relpath.suffix.lower() in {".htm", ".html"} else None
    integrity = inspect_file(destination, expected_kind)
    archived.size_bytes = integrity.size_bytes
    archived.sha256 = integrity.sha256
    archived.download_status = integrity.status
    if integrity.valid:
        archived.local_relpath = relpath.as_posix()
        archived.verified_at = datetime.now(timezone.utc)
    session.add(archived)
    return FileManifest(
        attachment_key=attachment.attachment_key,
        original_name=attachment.filename,
        local_relpath=relpath.as_posix() if integrity.valid else None,
        file_role=role,
        source_container_key=container_key,
        size_bytes=integrity.size_bytes,
        sha256=integrity.sha256,
        download_status=integrity.status,
    )


def _attachment_relname(attachment) -> str:
    original = PurePosixPath(safe_filename(attachment.filename))
    suffix = original.suffix
    stem = original.name[:-len(suffix)] if suffix else original.name
    key_digest = hashlib.sha256(attachment.attachment_key.encode("utf-8")).hexdigest()[:12]
    return safe_filename(f"{stem}_{key_digest}{suffix}")
