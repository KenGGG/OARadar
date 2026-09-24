"""Read-only, per-item Markdown delivery evidence shared by the business views."""
from __future__ import annotations

from collections import defaultdict
from typing import Any
from collections.abc import Callable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from oa_knowledge.db.models import (
    ArchivedFile, ClassificationDecision, MarkdownExport, MarkdownTask,
    OAItem, OAManifestItem, ParseJob,
)
from oa_knowledge.source_roles import MARKDOWN_SOURCE_ROLES


def delivery_facts(session: Session, item: OAItem) -> dict[str, Any]:
    return delivery_facts_map(session, [item])[item.id]


def delivery_facts_map(
    session: Session, items: list[OAItem] | None = None,
    *, file_exists: Callable[[str], bool] | None = None,
) -> dict[int, dict[str, Any]]:
    """Load facts in batches; legacy exports may identify only their source file."""
    if items is None:
        items = list(session.scalars(select(OAItem).where(OAItem.source_channel == "done")))
    if not items:
        return {}
    item_ids = [item.id for item in items]
    keys = [item.oa_item_key for item in items]
    manifests = {row.oa_item_key: row for row in session.execute(
        select(OAManifestItem.oa_item_key, OAManifestItem.processing_status,
               OAManifestItem.no_attachment_confirmed).where(OAManifestItem.oa_item_key.in_(keys))
    )}
    decisions = {row.oa_item_key: row for row in session.execute(
        select(ClassificationDecision.oa_item_key, ClassificationDecision.classification_status).where(
            ClassificationDecision.oa_item_key.in_(keys), ClassificationDecision.is_current.is_(True),
        )
    )}
    # "primary" is the pre-role-migration source attachment role.
    files = list(session.execute(select(ArchivedFile.id, ArchivedFile.oa_item_id, ArchivedFile.sha256).where(
        ArchivedFile.oa_item_id.in_(item_ids),
        ArchivedFile.file_role.in_((*MARKDOWN_SOURCE_ROLES, "primary")),
    )))
    file_ids = {file.id for file in files}
    files_by_item: dict[int, list] = defaultdict(list)
    for file in files:
        files_by_item[file.oa_item_id].append(file)
    exports_by_file: dict[int, list] = defaultdict(list)
    indexes_by_item: dict[int, list] = defaultdict(list)
    for export in session.execute(select(
        MarkdownExport.oa_item_id, MarkdownExport.source_file_id, MarkdownExport.document_kind,
        MarkdownExport.status, MarkdownExport.source_sha256, MarkdownExport.markdown_relpath,
    ).where(or_(
        MarkdownExport.oa_item_id.in_(item_ids), MarkdownExport.source_file_id.in_(file_ids),
    )).order_by(MarkdownExport.updated_at.desc(), MarkdownExport.id.desc())):
        if export.document_kind == "item_index":
            indexes_by_item[export.oa_item_id].append(export)
        elif export.source_file_id in file_ids:
            exports_by_file[export.source_file_id].append(export)
    jobs_by_file: dict[int, list] = defaultdict(list)
    for job in session.execute(select(ParseJob.file_id, ParseJob.status).where(ParseJob.file_id.in_(file_ids))):
        jobs_by_file[job.file_id].append(job)
    for task in session.execute(select(MarkdownTask.source_file_id, MarkdownTask.status).where(MarkdownTask.source_file_id.in_(file_ids))):
        jobs_by_file[task.source_file_id].append(task)

    result = {}
    for item in items:
        manifest = manifests.get(item.oa_item_key)
        decision = decisions.get(item.oa_item_key)
        processing = manifest.processing_status if manifest else item.pipeline_status
        classification = decision.classification_status if decision else "unknown"
        indexes = indexes_by_item[item.id]
        index = indexes[0] if indexes else None
        sources = files_by_item[item.id]
        successful = unsupported = failed = running = 0
        for file in sources:
            exports = exports_by_file[file.id]
            current_export = exports[0] if exports else None
            if current_export and current_export.status == "success" and (not file.sha256 or current_export.source_sha256 == file.sha256) and (
                file_exists is None or file_exists(current_export.markdown_relpath)):
                successful += 1
                continue  # A repaired delivery supersedes previous parser attempts.
            statuses = {row.status for row in jobs_by_file[file.id]}
            if current_export:
                statuses.add(current_export.status)
            unsupported += bool(statuses & {"unsupported", "skipped"})
            failed += "failed" in statuses
            running += "running" in statuses
        index_status = index.status if index else "pending"
        if index and index_status == "success" and file_exists is not None and not file_exists(index.markdown_relpath):
            index_status = "pending"
        index_failed = index_status == "failed"
        has_index = index_status == "success"
        no_attachment = bool(manifest and processing == "no_attachment" and manifest.no_attachment_confirmed)
        if processing == "depth_limit_reached":
            status, reason = "needs_review", "容器层级超过上限，需人工确认"
        elif processing == "no_attachment" and not no_attachment:
            status, reason = "needs_review", "未发现附件，但缺少无附件确认，请重新核对归档"
        elif processing == "skipped" or classification == "excluded":
            status, reason = "excluded", "已按规则排除"
        elif classification == "needs_review":
            status, reason = "needs_review", "分类需要人工复核"
        elif failed or index_failed:
            status, reason = "failed", "Markdown 转换或索引交付失败"
        elif unsupported:
            status, reason = "partial", "存在暂不支持转换的附件"
        elif running or index_status == "running":
            status, reason = "working", "正在转换 Markdown"
        elif manifest and processing not in {"downloaded", "no_attachment"}:
            status, reason = "pending", "等待原件归档核验"
        elif has_index and successful == len(sources) and (sources or no_attachment):
            status, reason = "complete", None
        elif successful or (has_index and sources):
            status, reason = "partial", "附件 Markdown 或事项索引尚未全部交付"
        else:
            status, reason = "pending", "等待附件交付证据或无附件确认" if has_index else "等待 Markdown 交付"
        result[item.id] = {
            "status": status, "expected": len(sources), "successful": successful,
            "unsupported": unsupported, "failed": failed,
            "index_status": index_status,
            "index_relpath": index.markdown_relpath if index and has_index else None,
            "classification_status": classification, "reason": reason,
        }
    return result
