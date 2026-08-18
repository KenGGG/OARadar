"""Deterministic Markdown projections backed by lifecycle and parse records."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath, Path

from sqlalchemy import select
from sqlalchemy.orm import Session
import yaml

from oa_knowledge.archive import atomic_write_bytes
from oa_knowledge.db.models import (
    ArchivedFile,
    ContentObject,
    ItemSnapshot,
    KnowledgeDocument,
    LogicalItem,
    OAItem,
    OAItemDocumentRelation,
    ParseArtifact,
    SourceAttachment,
    SourceReference,
)
from oa_knowledge.obsidian.source_note import OBSIDIAN_PROFILE, OBSIDIAN_PROFILE_REVISION


@dataclass(frozen=True)
class ProjectionResult:
    oa_overview_relpath: str
    knowledge_relpaths: tuple[str, ...]


def _frontmatter(payload: dict) -> str:
    return "---\n" + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).rstrip() + "\n---\n"


def publish_pending_projection(session: Session, logical_item_id: int, data_root: Path) -> ProjectionResult:
    logical = session.get(LogicalItem, logical_item_id)
    if logical is None:
        raise LookupError(f"logical item not found: {logical_item_id}")
    if logical.lifecycle_status not in {"identity_pending", "done_confirmed"}:
        raise ValueError("lifecycle projection requires pending or done-confirmed state")
    publication_status = "draft_pending" if logical.lifecycle_status == "identity_pending" else "done_confirmed"
    snapshot = session.scalar(select(ItemSnapshot).where(
        ItemSnapshot.logical_item_id == logical.id,
        ItemSnapshot.snapshot_kind.in_(("pending_initial", "pending_updated")),
    ).order_by(ItemSnapshot.id.desc()).limit(1))
    if snapshot is None:
        raise ValueError("pending projection requires a real lifecycle snapshot")
    item = session.scalar(select(OAItem).where(
        OAItem.logical_item_id == logical.id,
        OAItem.source_channel == "pending",
    ).order_by(OAItem.id.desc()).limit(1))
    if item is None:
        raise ValueError("pending projection requires a persisted pending OA item")
    sources = session.scalars(select(SourceAttachment).where(
        SourceAttachment.snapshot_id == snapshot.id,
        SourceAttachment.download_status == "verified",
        SourceAttachment.content_object_id.is_not(None),
    ).order_by(SourceAttachment.ordinal, SourceAttachment.id)).all()

    links: list[tuple[SourceAttachment, KnowledgeDocument, str]] = []
    source_evidence: list[tuple[SourceAttachment, ArchivedFile | None]] = []
    relpaths: list[str] = []
    for source in sources:
        source_file = session.get(ArchivedFile, source.source_file_id) if source.source_file_id else None
        source_evidence.append((source, source_file))
        content = session.get(ContentObject, source.content_object_id)
        if content is None or content.active_parse_artifact_id is None:
            continue
        artifact = session.get(ParseArtifact, content.active_parse_artifact_id)
        if artifact is None or artifact.lifecycle_status != "valid":
            continue
        parsed_path = data_root / "parse" / artifact.output_relpath
        if not parsed_path.is_file():
            continue
        document = session.scalar(select(KnowledgeDocument).where(
            KnowledgeDocument.content_object_id == content.id,
        ))
        if document is None:
            document = KnowledgeDocument(
                knowledge_key=f"content-{content.id}",
                content_object_id=content.id,
                title=source.display_title or source.original_name,
                publish_status=publication_status,
            )
            session.add(document)
            session.flush()
        document.active_parse_artifact_id = artifact.id
        document.publish_status = publication_status
        relpath = PurePosixPath("vault", "知识文档", f"kd-{document.id}.md")
        document.vault_relpath = relpath.as_posix()
        body = parsed_path.read_text(encoding="utf-8", errors="replace")
        metadata = {
            "id": f"knowledge-{document.id}",
            "title": document.title,
            "note_type": "knowledge_document",
            "publication_status": publication_status,
            "lifecycle_status": logical.lifecycle_status,
            "content_sha256": content.sha256,
            "parse_artifact_id": artifact.id,
            "parse_engine": artifact.engine,
            "quality_score": artifact.quality_score,
            "source_oa": f"[[OA事项/li-{logical.id}]]",
            "obsidian_profile": OBSIDIAN_PROFILE,
            "obsidian_profile_revision": OBSIDIAN_PROFILE_REVISION,
            "tags": ["oa/attachment", f"lifecycle/{logical.lifecycle_status}", f"publication/{publication_status}"],
        }
        atomic_write_bytes((_frontmatter(metadata) + "\n" + body).encode("utf-8"), data_root, relpath)
        if source_file is not None:
            reference = session.scalar(select(SourceReference).where(
                SourceReference.knowledge_document_id == document.id,
                SourceReference.source_file_id == source_file.id,
            ))
            if reference is None:
                session.add(SourceReference(
                    knowledge_document_id=document.id,
                    source_file_id=source_file.id,
                    oa_item_id=item.id,
                ))
        relation = session.scalar(select(OAItemDocumentRelation).where(
            OAItemDocumentRelation.logical_item_id == logical.id,
            OAItemDocumentRelation.knowledge_document_id == document.id,
            OAItemDocumentRelation.source_attachment_id == source.id,
        ))
        if relation is None:
            session.add(OAItemDocumentRelation(
                logical_item_id=logical.id,
                knowledge_document_id=document.id,
                source_attachment_id=source.id,
                ordinal=source.ordinal,
                role=source.role,
                is_main_document=source.is_main_document,
                display_title=source.display_title or source.original_name,
            ))
        links.append((source, document, relpath.as_posix()))
        relpaths.append(relpath.as_posix())

    overview_relpath = PurePosixPath("vault", "OA事项", f"li-{logical.id}.md")
    overview_metadata = {
        "id": f"oa-item-{logical.id}",
        "title": logical.title,
        "note_type": "oa_item_overview",
        "publication_status": publication_status,
        "lifecycle_status": logical.lifecycle_status,
        "snapshot_id": snapshot.id,
        "source_channel": "pending",
        "obsidian_profile": OBSIDIAN_PROFILE,
        "obsidian_profile_revision": OBSIDIAN_PROFILE_REVISION,
        "tags": ["oa/item", f"lifecycle/{logical.lifecycle_status}", f"publication/{publication_status}"],
    }
    notice = (
        ["> [!warning] 待办草稿", "> 本文来自只读 Pending 快照，尚未经过 Done 对账，不是最终归档。"]
        if logical.lifecycle_status == "identity_pending" else
        ["> [!info] 已办已确认", "> Pending 与 Done 已通过稳定主键对账；done_final 下载与正式知识准入仍可继续完善。"]
    )
    lines = [f"# {logical.title}", "", *notice, "", "## 事项概括", "", "待本地 qwen3.5:9b 摘要任务生成。", "", "## 附件"]
    if links:
        for source, document, _ in links:
            lines.append(f"{source.ordinal}. [[知识文档/kd-{document.id}|{source.display_title or source.original_name}]]")
    else:
        lines.append("暂无通过解析质量门禁的附件知识文档。")
    lines.extend(["", "## 来源附件证据", ""])
    if source_evidence:
        for source, source_file in source_evidence:
            local = source_file.local_relpath if source_file and source_file.local_relpath else "尚无本地文件"
            lines.append(f"{source.ordinal}. {source.display_title or source.original_name} · `{source.download_status}` · `{local}`")
    else:
        lines.append("当前快照没有来源附件记录。")
    atomic_write_bytes((_frontmatter(overview_metadata) + "\n" + "\n".join(lines) + "\n").encode("utf-8"), data_root, overview_relpath)
    session.flush()
    return ProjectionResult(overview_relpath.as_posix(), tuple(relpaths))
