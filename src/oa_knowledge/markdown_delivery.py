"""Small, local-only operations shared by the V2 Markdown Delivery stages."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.db.models import ArchivedFile, MarkdownExport, OAItem, ParseJob
from oa_knowledge.markdown_export.render import SCHEMA_VERSION
from oa_knowledge.source_markdown.service import _source_tree
from oa_knowledge.source_roles import MARKDOWN_SOURCE_ROLES


INTERNAL_CATEGORIES = (
    "公司治理", "经营管理", "业务项目", "风险管理",
    "财务资金", "人力行政", "信息化", "其他内部",
)
_CATEGORY_RULES = (
    ("风险管理", ("风险", "合规", "内控", "审计", "授信", "租后")),
    ("财务资金", ("财务", "预算", "资金", "报销", "会计", "税")),
    ("人力行政", ("人力", "招聘", "绩效", "行政", "党群", "工会")),
    ("信息化", ("信息化", "系统", "数据", "网络", "安全")),
    ("业务项目", ("项目", "客户", "业务", "投资", "融资", "合同")),
    ("公司治理", ("董事会", "股东", "治理", "章程", "决议")),
    ("经营管理", ("经营", "管理", "会议", "计划", "通知")),
)
_EXTERNAL_MARKERS = ("国资", "人民政府", "委员会", "监管", "银行", "税务", "法院", "厅", "局")


def _normalized_issuer(value: str | None) -> str | None:
    if not value:
        return None
    normalized = " ".join(value.split()).strip("：:;；，,。")
    return normalized or None


def classify_done_item(session: Session, oa_item_key: str) -> OAItem:
    """Apply deterministic V2 classification without creating classification history."""
    item = session.scalar(select(OAItem).where(
        OAItem.oa_item_key == oa_item_key,
        OAItem.source_channel == "done",
    ))
    if item is None:
        raise LookupError("done item not found")
    if item.classification_state == "confirmed":
        return item

    title = item.title or ""
    issuer = _normalized_issuer(item.sender)
    combined = f"{title} {issuer or ''} {item.document_number or ''}"
    external = bool(item.document_number) or any(marker in combined for marker in _EXTERNAL_MARKERS)
    internal = any(marker in combined for marker in ("本公司", "内部", "公司", "部门"))

    item.internal_category = None
    item.external_issuer = None
    if external and issuer:
        item.source_type = "external"
        item.external_issuer = issuer
    elif internal:
        item.source_type = "internal"
        item.internal_category = next(
            (category for category, keywords in _CATEGORY_RULES if any(word in combined for word in keywords)),
            "其他内部",
        )
    else:
        item.source_type = "unknown"
    item.classification_version = "v1"
    session.flush()
    return item


def publish_item_index(session: Session, settings, oa_item_key: str) -> Path:
    """Publish the stable, human-readable index for one Done item."""
    item = session.scalar(select(OAItem).where(
        OAItem.oa_item_key == oa_item_key,
        OAItem.source_channel == "done",
    ))
    if item is None or not item.archive_relpath:
        raise FileNotFoundError("done archive directory unavailable")
    tree = _source_tree(settings, item.archive_relpath)
    destination = settings.markdown_root.joinpath(*tree.parts, "_index.md")
    files = session.scalars(select(ArchivedFile).where(
        ArchivedFile.oa_item_id == item.id,
        ArchivedFile.file_role.in_(MARKDOWN_SOURCE_ROLES),
    ).order_by(ArchivedFile.id)).all()
    exports = {
        row.source_file_id: row
        for row in session.scalars(select(MarkdownExport).where(
            MarkdownExport.source_file_id.in_([file.id for file in files]),
        )).all()
    } if files else {}
    jobs = {
        row.file_id: row
        for row in session.scalars(select(ParseJob).where(
            ParseJob.file_id.in_([file.id for file in files]),
        )).all()
    } if files else {}

    frontmatter = {
        "title": item.title,
        "oa_item_key": item.oa_item_key,
        "source_type": item.source_type or "unknown",
        "internal_category": item.internal_category,
        "external_issuer": item.external_issuer,
        "classification_version": item.classification_version or "v1",
        "sender": item.sender,
        "initiated_at": item.initiated_at.isoformat() if item.initiated_at else None,
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
        "document_number": item.document_number,
    }
    lines = ["---"]
    lines.extend(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in frontmatter.items())
    lines.extend(["---", "", f"# {item.title}", "", "## 附件"])
    if not files:
        lines.append("\n- 无附件（已办归档证据已核验）。")
    for file in files:
        export = exports.get(file.id)
        job = jobs.get(file.id)
        if export and export.status == "success":
            target = settings.workspace_root / export.markdown_relpath
            relative = os.path.relpath(target, destination.parent).replace(os.sep, "/")
            lines.append(f"- [{file.original_name}]({relative})")
        elif job and job.status == "skipped":
            lines.append(f"- {file.original_name}：不支持转换")
        else:
            status = export.status if export else (job.status if job else "待解析")
            lines.append(f"- {file.original_name}：{status}")
    content = "\n".join(lines).rstrip() + "\n"
    content_hash = sha256(content.encode("utf-8")).hexdigest()
    record = session.scalar(select(MarkdownExport).where(
        MarkdownExport.oa_item_id == item.id,
        MarkdownExport.document_kind == "item_index",
        MarkdownExport.schema_version == SCHEMA_VERSION,
    ))
    current = destination.is_file() and destination.read_text(encoding="utf-8") == content
    if not current:
        destination.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=".oaradar-index-", dir=destination.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    if record is not None and record.status == "success" and record.markdown_sha256 == content_hash:
        return destination
    record = record or MarkdownExport(
        oa_item_id=item.id,
        document_kind="item_index",
        source_relpath=item.archive_relpath,
        markdown_relpath=destination.relative_to(settings.workspace_root).as_posix(),
        source_sha256=sha256(item.oa_item_key.encode("utf-8")).hexdigest(),
        parse_engine="item_index",
        parse_engine_version="v1",
        parse_config_hash="v1",
        schema_version=SCHEMA_VERSION,
        status="pending",
    )
    if record.id is None:
        session.add(record)
    record.oa_item_id = item.id
    record.document_kind = "item_index"
    record.source_file_id = None
    record.content_object_id = None
    record.parse_artifact_id = None
    record.source_relpath = item.archive_relpath
    record.markdown_relpath = destination.relative_to(settings.workspace_root).as_posix()
    record.source_sha256 = sha256(item.oa_item_key.encode("utf-8")).hexdigest()
    record.parse_engine = "item_index"
    record.parse_engine_version = "v1"
    record.parse_config_hash = "v1"
    record.status = "success"
    record.markdown_sha256 = content_hash
    record.last_error_code = None
    record.last_error = None
    record.generated_at = datetime.now(timezone.utc)
    session.flush()
    return destination
