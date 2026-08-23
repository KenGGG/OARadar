"""Read-only lifecycle views for the four-entry WebUI."""

from __future__ import annotations

import json
import sqlite3
from urllib.request import urlopen

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import (
    ArchivedFile, ArchiveMember, ArchivePackage, ContentObject, ItemOccurrence, ItemSnapshot,
    KnowledgeDocument, LogicalItem, NotificationDelivery, OAItem, OAItemDocumentRelation, OperationJob, ParseArtifact,
    SourceAttachment, SourceReference, OperationEvent, OAManifestItem, PipelineTask, ResourceLease, SummaryVersion,
)


def _counts(session: Session, model, *conditions) -> int:
    return session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0


def processing_center(settings: Settings) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            queues = {}
            for name in ("realtime_pending", "realtime_done", "historical_done_backfill"):
                queues[name] = {status: _counts(session, PipelineTask, PipelineTask.queue_name == name, PipelineTask.status == status)
                                for status in ("queued", "running", "completed", "failed")}
            control = session.scalar(select(PipelineTask.status).where(PipelineTask.idempotency_key == "__historical_control__"))
            task_order = case(
                (PipelineTask.status == "running", 0), (PipelineTask.status == "failed", 1),
                (PipelineTask.queue_name == "realtime_pending", 2), (PipelineTask.queue_name == "realtime_done", 3),
                else_=4,
            )
            tasks = session.scalars(select(PipelineTask).where(PipelineTask.idempotency_key != "__historical_control__")
                                    .order_by(task_order, PipelineTask.id.desc()).limit(100)).all()
            logical_titles = {row.id: row.title for row in session.scalars(select(LogicalItem).where(
                LogicalItem.id.in_([task.logical_item_id for task in tasks if task.logical_item_id])
            )).all()}
            manifest_titles = {row.oa_item_key: row.title for row in session.scalars(select(OAManifestItem).where(
                OAManifestItem.oa_item_key.in_([task.logical_item_key for task in tasks if task.queue_name != "realtime_pending"])
            )).all()}
            leases = session.scalars(select(ResourceLease).order_by(ResourceLease.id)).all()
            historical_state = (
                "paused" if control == "paused" else
                "running" if queues["historical_done_backfill"]["running"] else
                "queued" if queues["historical_done_backfill"]["queued"] else
                "idle"
            )
            return {"queues": queues, "historical_paused": control == "paused", "historical_state": historical_state, "mock_data": False,
                    "gpu_leases": [{"resource": x.resource_key, "kind": x.resource_kind, "owner": x.owner_id,
                                    "acquired_at": x.acquired_at.isoformat(), "expires_at": x.expires_at.isoformat()} for x in leases],
                    "tasks": [{"id": x.id, "queue": x.queue_name, "stage": x.stage, "status": x.status,
                               "logical_item_key": x.logical_item_key, "progress_current": x.progress_current,
                               "title": logical_titles.get(x.logical_item_id) or manifest_titles.get(x.logical_item_key) or x.logical_item_key,
                               "progress_total": x.progress_total, "attempts": x.attempts, "error_code": x.error_code,
                               "recoverable": x.recoverable, "created_at": x.created_at.isoformat()} for x in tasks]}
    finally:
        engine.dispose()


def pending_list(settings: Settings) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            rows = session.scalars(select(ItemOccurrence).where(
                ItemOccurrence.channel == "pending",
                ItemOccurrence.occurrence_status == "active",
            ).order_by(ItemOccurrence.received_at.desc(), ItemOccurrence.id)).all()
            items = []
            for row in rows:
                snapshot = session.scalar(select(ItemSnapshot).where(
                    ItemSnapshot.logical_item_id == row.logical_item_id,
                ).order_by(ItemSnapshot.id.desc()).limit(1))
                sources = session.scalars(select(SourceAttachment).where(
                    SourceAttachment.snapshot_id == snapshot.id,
                )).all() if snapshot else []
                item = session.get(OAItem, row.oa_item_id) if row.oa_item_id else None
                files = item.files if item else []
                summary = session.scalar(select(SummaryVersion).where(
                    SummaryVersion.logical_item_id == row.logical_item_id, SummaryVersion.summary_kind == "pending",
                ).order_by(SummaryVersion.id.desc()).limit(1))
                delivery = session.scalar(select(NotificationDelivery).where(
                    NotificationDelivery.logical_item_id == row.logical_item_id,
                    NotificationDelivery.channel == "feishu",
                    NotificationDelivery.notification_type == "pending_summary",
                ).order_by(NotificationDelivery.id.desc()).limit(1))
                items.append({
                    "id": row.id, "logical_item_id": row.logical_item_id, "title": row.title,
                    "sender": row.sender, "received_at": row.received_at.isoformat() if row.received_at else None,
                    "current_node": row.current_node, "processing_status": row.processing_status,
                    "list_discovered": True,
                    "identity_captured": bool(row.summary_id_text and row.process_id_text and row.workitem_id_text),
                    "snapshot_kind": snapshot.snapshot_kind if snapshot else None,
                    "snapshot_id": snapshot.id if snapshot else None,
                    "body_status": "captured" if any(f.file_role == "body_snapshot" and f.download_status == "verified" for f in files) else "pending",
                    "workflow_status": "captured" if any(f.file_role == "workflow_snapshot" and f.download_status == "verified" for f in files) else "pending",
                    "opinion_status": "captured_with_workflow" if any(f.file_role == "workflow_snapshot" and f.download_status == "verified" for f in files) else "pending",
                    "attachment_total": len(sources),
                    "attachment_verified": sum(source.download_status == "verified" for source in sources),
                    "attachment_failed": sum(source.download_status != "verified" for source in sources),
                    "last_synced_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
                    "last_discovered_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
                    "last_summary_at": summary.created_at.isoformat() if summary and summary.created_at else None,
                    "feishu_status": delivery.status if delivery else None,
                    "last_notified_at": (delivery.sent_at or delivery.updated_at).isoformat() if delivery and (delivery.sent_at or delivery.updated_at) else None,
                    "notify_error_code": delivery.error_code if delivery else None,
                    "ollama_summary_status": summary.status if summary else "queued" if _counts(session, PipelineTask, PipelineTask.logical_item_id == row.logical_item_id, PipelineTask.stage == "pending_summary") else "pending",
                })
            return {"items": items, "total": len(items)}
    finally:
        engine.dispose()


def pending_detail(settings: Settings, occurrence_id: int) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            row = session.get(ItemOccurrence, occurrence_id)
            if row is None or row.channel != "pending":
                raise LookupError("pending occurrence not found")
            snapshot = session.scalar(select(ItemSnapshot).where(
                ItemSnapshot.logical_item_id == row.logical_item_id,
            ).order_by(ItemSnapshot.id.desc()).limit(1))
            item = session.get(OAItem, row.oa_item_id) if row.oa_item_id else None
            summary = session.scalar(select(SummaryVersion).where(
                SummaryVersion.logical_item_id == row.logical_item_id, SummaryVersion.summary_kind == "pending",
            ).order_by(SummaryVersion.id.desc()).limit(1))
            sources = session.scalars(select(SourceAttachment).where(
                SourceAttachment.snapshot_id == snapshot.id,
            ).order_by(SourceAttachment.ordinal)).all() if snapshot else []
            attachments = []
            for source in sources:
                file = session.get(ArchivedFile, source.source_file_id) if source.source_file_id else None
                package = session.scalar(select(ArchivePackage).where(ArchivePackage.source_attachment_id == source.id))
                members = session.scalars(select(ArchiveMember).where(
                    ArchiveMember.archive_package_id == package.id,
                )).all() if package else []
                document = session.scalar(select(KnowledgeDocument).where(
                    KnowledgeDocument.content_object_id == source.content_object_id,
                )) if source.content_object_id else None
                attachments.append({
                    "id": source.id, "ordinal": source.ordinal, "role": source.role,
                    "name": source.display_title or source.original_name,
                    "status": source.download_status, "error_code": source.error_code,
                    "retry_count": source.retry_count,
                    "size_bytes": file.size_bytes if file else None,
                    "sha256": file.sha256 if file else None,
                    "local_relpath": file.local_relpath if file else None,
                    "content_reused": bool(source.content_object_id and _counts(session, ArchivedFile, ArchivedFile.content_object_id == source.content_object_id) > 1),
                    "knowledge_document_id": document.id if document else None,
                    "archive": {
                        "id": package.id, "format": package.archive_format, "status": package.status,
                        "security_status": package.security_status, "error_code": package.error_code,
                        "tree": json.loads(package.tree_json or "[]"),
                        "members": [{"id": m.id, "path": m.original_path, "status": m.status, "error_code": m.error_code, "retry_count": m.retry_count} for m in members],
                    } if package else None,
                })
            files = [{"id": f.id, "role": f.file_role, "status": f.download_status, "local_relpath": f.local_relpath} for f in (item.files if item else []) if f.file_role in {"body_snapshot", "workflow_snapshot"}]
            return {
                "id": row.id, "logical_item_id": row.logical_item_id, "title": row.title,
                "sender": row.sender, "current_node": row.current_node,
                "identity": {"summary": bool(row.summary_id_text), "process": bool(row.process_id_text), "workitem": bool(row.workitem_id_text)},
                "snapshot": {"id": snapshot.id, "kind": snapshot.snapshot_kind, "version": snapshot.version, "payload": json.loads(snapshot.payload_json)} if snapshot else None,
                "evidence_files": files, "attachments": attachments,
                "lifecycle_pilot_status": "waiting_for_user_completion",
                "ollama_summary": json.loads(summary.structured_json) if summary else None,
                "ollama_summary_status": summary.status if summary else "pending",
            }
    finally:
        engine.dispose()


def done_list(
    settings: Settings,
    *,
    page: int = 1,
    page_size: int = 100,
    query: str | None = None,
) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            statement = select(OAManifestItem)
            if query:
                pattern = f"%{query.strip()}%"
                statement = statement.where(
                    OAManifestItem.title.ilike(pattern) | OAManifestItem.sender.ilike(pattern)
                )
            total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
            oa_done_total = _counts(session, OAManifestItem)
            downloaded_items = session.scalar(
                select(func.count(func.distinct(OAManifestItem.id))).select_from(OAManifestItem)
                .join(OAItem, OAItem.oa_item_key == OAManifestItem.oa_item_key)
                .where(OAManifestItem.processing_status == "downloaded")
            ) or 0
            verified_attachments = session.scalar(
                select(func.count(ArchivedFile.id)).select_from(ArchivedFile)
                .join(OAItem, OAItem.id == ArchivedFile.oa_item_id)
                .where(
                    OAItem.source_channel == "done",
                    ArchivedFile.download_status == "verified",
                    ArchivedFile.file_role.in_(("direct_attachment", "official_attachment", "opinion_attachment")),
                )
            ) or 0
            rows = session.scalars(statement.order_by(
                OAManifestItem.list_page, OAManifestItem.list_ordinal, OAManifestItem.id,
            ).offset((page - 1) * page_size).limit(page_size)).all()
            items = []
            for row in rows:
                archived = session.scalar(select(OAItem).where(OAItem.oa_item_key == row.oa_item_key))
                items.append({
                    "id": row.id, "item_id": row.workitem_id_text, "title": row.title, "sender": row.sender,
                    "initiated_at": row.initiated_at.isoformat() if row.initiated_at else None,
                    "completed_at": row.completed_at.isoformat() if row.completed_at else None,
                    "pipeline_status": row.processing_status,
                    "archive_relpath": row.archive_relpath or (archived.archive_relpath if archived else None),
                    "file_count": sum(
                        file.download_status == "verified" and file.file_role in {
                            "direct_attachment", "official_attachment", "opinion_attachment",
                        }
                        for file in archived.files
                    ) if archived else None,
                })
            return {
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
                "metrics": {
                    "oa_done_total": oa_done_total,
                    "downloaded_items": downloaded_items,
                    "verified_attachments": verified_attachments,
                },
                "lifecycle_pilot_status": "validated" if oa_done_total else "waiting_for_user_completion",
            }
    finally:
        engine.dispose()


def knowledge_list(settings: Settings) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            rows = session.scalars(select(KnowledgeDocument).order_by(KnowledgeDocument.updated_at.desc(), KnowledgeDocument.id.desc())).all()
            return {"documents": [{"id": row.id, "title": row.title, "publish_status": row.publish_status, "vault_relpath": row.vault_relpath, "active_parse_artifact_id": row.active_parse_artifact_id, "source_count": _counts(session, SourceReference, SourceReference.knowledge_document_id == row.id)} for row in rows], "total": len(rows)}
    finally:
        engine.dispose()


def knowledge_detail(settings: Settings, document_id: int) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            row = session.get(KnowledgeDocument, document_id)
            if row is None:
                raise LookupError("knowledge document not found")
            artifact = session.get(ParseArtifact, row.active_parse_artifact_id) if row.active_parse_artifact_id else None
            refs = session.scalars(select(SourceReference).where(SourceReference.knowledge_document_id == row.id)).all()
            preview = ""
            if row.vault_relpath and (settings.data_root / row.vault_relpath).is_file():
                preview = (settings.data_root / row.vault_relpath).read_text(encoding="utf-8", errors="replace")[:12000]
            return {"id": row.id, "title": row.title, "publish_status": row.publish_status, "vault_relpath": row.vault_relpath, "preview": preview, "artifact": {"id": artifact.id, "engine": artifact.engine, "quality_score": artifact.quality_score, "status": artifact.lifecycle_status, "output_relpath": artifact.output_relpath} if artifact else None, "sources": [{"oa_item_id": ref.oa_item_id, "source_file_id": ref.source_file_id} for ref in refs]}
    finally:
        engine.dispose()


def system_view(settings: Settings) -> dict:
    from oa_knowledge.markdown_export.service import markdown_status
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            worker = session.scalar(select(OperationJob).where(OperationJob.status.in_(("queued", "running"))).order_by(OperationJob.id.desc()).limit(1))
            schema = session.connection().exec_driver_sql("select version_num from alembic_version").scalar_one()
            integrity = session.connection().exec_driver_sql("pragma integrity_check").scalar_one()
            counts = {
                "pending": _counts(session, ItemOccurrence, ItemOccurrence.channel == "pending", ItemOccurrence.occurrence_status == "active"),
                "done": _counts(session, OAManifestItem),
                "files": _counts(session, ArchivedFile), "snapshots": _counts(session, ItemSnapshot),
                "source_attachments": _counts(session, SourceAttachment), "archive_packages": _counts(session, ArchivePackage),
                "archive_members": _counts(session, ArchiveMember), "parse_artifacts": _counts(session, ParseArtifact),
                "knowledge_documents": _counts(session, KnowledgeDocument),
                "failed_or_retry": _counts(session, SourceAttachment, SourceAttachment.download_status != "verified") + _counts(session, ArchiveMember, ArchiveMember.status != "verified"),
            }
            try:
                with urlopen(f"{settings.mineru.api_url}/health", timeout=settings.mineru.health_timeout_seconds) as response:
                    mineru = json.loads(response.read().decode("utf-8"))
            except Exception:
                mineru = {"status": "unavailable"}
            worker_payload = None
            if worker:
                latest = session.scalar(select(OperationEvent).where(
                    OperationEvent.job_id == worker.id,
                ).order_by(OperationEvent.sequence.desc()).limit(1))
                failures = _counts(
                    session, OperationEvent,
                    OperationEvent.job_id == worker.id,
                    OperationEvent.event_type == "item",
                    OperationEvent.status == "failed",
                )
                details = json.loads(latest.details_json or "{}") if latest else {}
                worker_payload = {
                    "id": worker.id, "type": worker.job_type, "status": worker.status,
                    "progress_current": worker.progress_current, "progress_total": worker.progress_total,
                    "current_title": details.get("title"),
                    "attachment_verified": details.get("attachment_verified", 0),
                    "attachment_total": details.get("attachment_total", 0),
                    "failure_count": failures,
                }
            return {"web": {"status": "running", "url": f"http://{settings.web.host}:{settings.web.port}"}, "worker": worker_payload, "mineru": mineru, "sqlite": {"schema": schema, "integrity": integrity}, "counts": counts,
                    "markdown": markdown_status(settings), "paths": {"archive": str(settings.archive_root), "markdown": str(settings.markdown_root)}}
    finally:
        engine.dispose()
