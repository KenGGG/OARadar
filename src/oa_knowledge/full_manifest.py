from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oa_knowledge.collector.done import DiscoveredDoneItem, DoneDiscovery
from oa_knowledge.db.models import ArchivedFile, OAItem, OAManifestItem, OAManifestSync


MANIFEST_STATUSES = {
    "discovered", "skipped", "pending_download", "processing",
    "downloaded", "no_attachment", "download_failed",
}
ATTACHMENT_ROLES = {"direct_attachment", "official_attachment", "official_body"}


def synchronize_manifest(session: Session, discovery: DoneDiscovery) -> OAManifestSync:
    """Upsert every discovered list row before any detail page is opened."""
    now = datetime.now(timezone.utc)
    source_total = discovery.source_total_count
    source_pages = discovery.source_total_pages
    if source_total is None or source_pages is None:
        raise ValueError("OA source totals are required for full-manifest reconciliation")

    upsert_manifest_page(session, discovery.items, now)
    session.flush()
    return finalize_manifest_sync(session, discovery, now)


def upsert_manifest_page(
    session: Session,
    items: tuple[DiscoveredDoneItem, ...] | list[DiscoveredDoneItem],
    synced_at: datetime | None = None,
) -> None:
    """Persist one OA list page before any detail work for that page starts."""
    now = synced_at or datetime.now(timezone.utc)
    for source in items:
        archived = session.scalar(select(OAItem).where(OAItem.oa_item_key == source.oa_item_key))
        if archived is not None and source.created_at is not None:
            archived.initiated_at = source.created_at
        row = session.scalar(select(OAManifestItem).where(OAManifestItem.oa_item_key == source.oa_item_key))
        if row is None:
            row = OAManifestItem(
                oa_item_key=source.oa_item_key,
                workitem_id_text=source.workitem_id_text or None,
                title=source.title,
                sender=source.sender,
                initiated_at=source.created_at,
                completed_at=source.completed_at,
                list_page=source.list_page,
                first_seen_at=now,
                last_synced_at=now,
                processing_status="discovered",
            )
            session.add(row)
        else:
            row.workitem_id_text = source.workitem_id_text or row.workitem_id_text
            row.title = source.title
            row.sender = source.sender
            row.initiated_at = source.created_at or row.initiated_at
            row.completed_at = source.completed_at
            row.list_page = source.list_page
            row.last_synced_at = now
    session.flush()


def finalize_manifest_sync(session: Session, discovery: DoneDiscovery, started_at: datetime | None = None) -> OAManifestSync:
    source_total = discovery.source_total_count
    source_pages = discovery.source_total_pages
    if source_total is None or source_pages is None:
        raise ValueError("OA source totals are required for full-manifest reconciliation")
    local_count = session.scalar(select(func.count()).select_from(OAManifestItem)) or 0
    complete = (
        local_count == source_total
        and len(discovery.items) == source_total
        and discovery.scanned_row_count >= source_total
        and discovery.pages_scanned == source_pages
    )
    sync = OAManifestSync(
        oa_total_count=source_total,
        local_manifest_count=local_count,
        pages_scanned=discovery.pages_scanned,
        source_total_pages=source_pages,
        status="manifest_complete" if complete else "manifest_incomplete",
        started_at=started_at or datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    session.add(sync)
    session.flush()
    return sync


def classify_manifest(session: Session, keywords: tuple[str, ...], data_root: Path) -> dict[str, int]:
    latest = session.scalar(select(OAManifestSync).order_by(OAManifestSync.id.desc()).limit(1))
    if latest is None or latest.status != "manifest_complete":
        raise ValueError("manifest_incomplete: classification/download is blocked until OA totals reconcile")
    classify_manifest_rows(session, session.scalars(select(OAManifestItem).order_by(OAManifestItem.id)).all(), keywords, data_root)
    session.flush()
    return manifest_counts(session)


def classify_manifest_rows(session: Session, rows: list[OAManifestItem], keywords: tuple[str, ...], data_root: Path) -> None:
    """Classify a persisted page and verify reuse without requiring final reconciliation."""
    for row in rows:
        keyword = next((word for word in keywords if word and word in row.title), None)
        if keyword:
            row.processing_status = "skipped"
            row.matched_exclusion_keyword = keyword
            row.last_error = None
            row.failure_stage = None
            continue
        row.matched_exclusion_keyword = None
        if row.processing_status in {"skipped", "discovered", "processing"}:
            row.processing_status = "pending_download"
        if row.processing_status in {"downloaded", "no_attachment"} and not verified_archive_exists(session, row, data_root):
            row.processing_status = "download_failed"
            row.failure_stage = "local_verification"
            row.last_error = "verified archive file is missing"
        if row.processing_status in {"pending_download", "download_failed"}:
            reuse_existing_archive(session, row, data_root)
    session.flush()


def reuse_existing_archive(session: Session, row: OAManifestItem, data_root: Path) -> bool:
    item = session.scalar(select(OAItem).where(OAItem.oa_item_key == row.oa_item_key))
    if item is None or not item.archive_relpath:
        return False
    files = session.scalars(select(ArchivedFile).where(ArchivedFile.oa_item_id == item.id)).all()
    if not files or any(f.download_status != "verified" or not f.local_relpath or not (data_root / f.local_relpath).is_file() for f in files):
        return False
    row.archive_relpath = item.archive_relpath
    row.processing_status = "downloaded" if any(f.file_role in ATTACHMENT_ROLES for f in files) else "no_attachment"
    row.last_error = None
    row.failure_stage = None
    return True


def verified_archive_exists(session: Session, row: OAManifestItem, data_root: Path) -> bool:
    if not row.archive_relpath:
        return False
    item = session.scalar(select(OAItem).where(OAItem.oa_item_key == row.oa_item_key))
    if item is None:
        return False
    files = session.scalars(select(ArchivedFile).where(ArchivedFile.oa_item_id == item.id)).all()
    return bool(files) and all(
        f.download_status == "verified" and f.local_relpath and (data_root / f.local_relpath).is_file()
        for f in files
    )


def archive_proxy(row: OAManifestItem) -> SimpleNamespace:
    """Shape expected by the existing read-only detail archiver."""
    return SimpleNamespace(
        oa_item_key=row.oa_item_key, workitem_id_text=row.workitem_id_text or "",
        title=row.title, sender=row.sender, created_at=None, completed_at=row.completed_at,
        category=None, oa_item_id=None, detail_url=None, archive_manifest_relpath=None,
        archive_status="processing", archived_at=None, last_error=None,
    )


def manifest_counts(session: Session) -> dict[str, int]:
    values = dict(session.execute(
        select(OAManifestItem.processing_status, func.count()).group_by(OAManifestItem.processing_status)
    ).all())
    total = sum(values.values())
    pending = values.get("discovered", 0) + values.get("pending_download", 0) + values.get("processing", 0)
    return {
        "local_manifest_count": total,
        "skipped": values.get("skipped", 0),
        "needs_download": total - values.get("skipped", 0),
        "downloaded": values.get("downloaded", 0),
        "no_attachment": values.get("no_attachment", 0),
        "download_failed": values.get("download_failed", 0),
        "auth_required": values.get("auth_required", 0),
        "pending": pending,
    }


def effective_exclusion_keywords(session: Session) -> tuple[str, ...]:
    """Return enabled title policies managed by the Web console."""
    from oa_knowledge.db.models import ExclusionPolicy
    values = [policy.pattern for policy in session.scalars(select(ExclusionPolicy).where(
        ExclusionPolicy.enabled.is_(True), ExclusionPolicy.action.in_(("skip", "metadata_only")), ExclusionPolicy.scope == "title",
    )).all()]
    return tuple(dict.fromkeys(value for value in values if value))


def export_manifest_csv(session: Session, data_root: Path) -> Path:
    destination = data_root / "runtime" / "reports" / "oa_manifest.csv"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("事项ID", "标题", "发起人", "办理时间", "处理状态", "命中的排除关键词", "是否需要下载", "本地归档目录", "重试次数", "最后错误"))
        for row in session.scalars(select(OAManifestItem).order_by(OAManifestItem.list_page, OAManifestItem.id)):
            writer.writerow((
                row.workitem_id_text or row.oa_item_key, row.title, row.sender or "",
                row.completed_at.isoformat(sep=" ") if row.completed_at else "", row.processing_status,
                row.matched_exclusion_keyword or "", "否" if row.processing_status == "skipped" else "是",
                row.archive_relpath or "", row.retry_count, row.last_error or "",
            ))
    temporary.replace(destination)
    return destination
