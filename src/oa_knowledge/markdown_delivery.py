"""Small, local-only operations shared by the V2 Markdown Delivery stages."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from oa_knowledge.db.models import ArchivedFile, ClassificationDecision, ContentObject, MarkdownExport, OAItem, ParseArtifact, ParseJob
from oa_knowledge.markdown_export.render import SCHEMA_VERSION
from oa_knowledge.source_markdown.service import _source_tree
from oa_knowledge.source_roles import MARKDOWN_SOURCE_ROLES
from oa_knowledge.archive.naming import safe_filename
from oa_knowledge.runtime_paths import resolve_cache_path


INTERNAL_CATEGORIES = (
    "公司治理", "经营管理", "业务项目", "风险管理",
    "财务资金", "人力行政", "信息化", "其他内部",
)
_DECISION_CATEGORY_DIRECTORIES = {
    "01_公司治理与决策": "公司治理",
    "02_业务项目与投放租后": "业务项目",
    "03_风险合规审计法务": "风险管理",
    "04_财务资金与融资": "财务资金",
    "05_经营计划与绩效考核": "经营管理",
    "06_人力资源": "人力行政",
    "07_党建纪检与工会": "人力行政",
    "08_行政采购与信息化": "信息化",
    "09_对外报送与监管反馈": "经营管理",
    "99_其他内部": "其他内部",
}
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
_DOCUMENT_NUMBER = re.compile(
    r"(?P<number>[A-Za-z\u4e00-\u9fff]{1,24}\s*[〔［【\[]\s*\d{4}\s*[〕］】\]]\s*\d{1,6}\s*号)"
)
_FORMAL_TITLE = re.compile(r"(?:关于|通知|函|决定|通报|批复|意见|办法|公告)")
_CITED_NUMBER = re.compile(r"(?:根据|按照|参照|转发|引用|贯彻|落实)[^\n]{0,60}$")
_DISPLAY_PREFIX = re.compile(r"^\s*(?:【(?:公告|以此为准|文件传阅|传阅)】\s*)+")
_RELAY_SUFFIX = re.compile(r"\s*[（(]由[^）)]+原发[）)]\s*$")


@dataclass(frozen=True, slots=True)
class DocumentNumberMatch:
    raw: str
    normalized: str
    quote: str


@dataclass(frozen=True, slots=True)
class ExternalDocumentMetadata:
    document_number: str | None
    canonical_issuer: str | None
    evidence: dict[str, object] | None


def _normalize_document_number(value: str | None) -> str | None:
    if not value:
        return None
    value = unicodedata.normalize("NFKC", value)
    match = _DOCUMENT_NUMBER.search(value)
    if match is None:
        return None
    raw = match.group("number")
    number = re.sub(r"\s+", "", raw).translate(str.maketrans({"[": "〔", "]": "〕", "【": "〔", "】": "〕"}))
    if number.startswith(("根据", "按照", "参照", "转发", "引用", "贯彻", "落实")):
        return None
    return number if _DOCUMENT_NUMBER.fullmatch(number) else None


def _extract_current_document_number(text: str) -> DocumentNumberMatch | None:
    """Accept one header-like current number; never use a cited reference."""
    normalized = unicodedata.normalize("NFKC", text)
    candidates: list[tuple[int, DocumentNumberMatch]] = []
    for match in _DOCUMENT_NUMBER.finditer(normalized):
        number = _normalize_document_number(match.group("number"))
        if number is None:
            continue
        before = normalized[max(0, match.start() - 80):match.start()]
        after = normalized[match.end():match.end() + 140]
        if _CITED_NUMBER.search(before):
            continue
        # A formal title immediately around the number is primary-document
        # evidence.  Numbers in an arbitrary later citation do not qualify.
        if not _FORMAL_TITLE.search(before + after):
            continue
        score = (500 if match.start() < 1800 else 0) + (100 if _FORMAL_TITLE.search(after) else 0) - match.start() // 200
        candidates.append((score, DocumentNumberMatch(match.group("number"), number, number)))
    if not candidates:
        return None
    best_score = max(score for score, _ in candidates)
    winners = [row for score, row in candidates if score == best_score]
    if len(winners) != 1:
        return None
    # Two title-bearing numbers close together signal an independent bundle,
    # not one OA-level main document.
    if sum(1 for _, row in candidates if row.normalized != winners[0].normalized) > 1:
        return None
    return winners[0]


def _display_title(item: OAItem, document_number: str | None) -> str:
    title = _DISPLAY_PREFIX.sub("", item.title or "未命名事项")
    title = _RELAY_SUFFIX.sub("", title).strip()
    if document_number:
        title = re.sub(re.escape(document_number), "", unicodedata.normalize("NFKC", title), count=1)
        title = re.sub(r"^[\s\-—:：]+", "", title).strip()
    return title or "未命名事项"


def _valid_artifact(session: Session, source: ArchivedFile) -> ParseArtifact | None:
    content = session.get(ContentObject, source.content_object_id) if source.content_object_id else None
    if content and content.active_parse_artifact_id:
        artifact = session.get(ParseArtifact, content.active_parse_artifact_id)
        if artifact and artifact.lifecycle_status == "valid":
            return artifact
    return session.scalar(select(ParseArtifact).where(
        ParseArtifact.content_object_id == source.content_object_id,
        ParseArtifact.lifecycle_status == "valid",
    ).order_by(ParseArtifact.quality_score.desc(), ParseArtifact.id.desc()).limit(1)) if source.content_object_id else None


def _sidecar_document_number(path: Path) -> tuple[DocumentNumberMatch, int | None] | None:
    """Use retained MinerU blocks only as a local fallback; never trigger parsing."""
    def walk(value, page=None):
        if isinstance(value, dict):
            page = value.get("page_idx", value.get("page_no", page))
            for key in ("text", "text_content", "content", "markdown"):
                if isinstance(value.get(key), str):
                    yield value[key], page
            for child in value.values():
                yield from walk(child, page)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child, page)
    for sidecar in path.parent.glob("*middle.json"):
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for text, page in walk(payload):
            if match := _extract_current_document_number(text):
                return match, page + 1 if isinstance(page, int) else None
    return None


def resolve_external_document_metadata(
    session: Session, settings, item: OAItem, decision: ClassificationDecision | None = None,
) -> ExternalDocumentMetadata:
    """Resolve one current external number from this item's existing evidence only."""
    decision = decision or require_current_classified_decision(session, item.oa_item_key)
    if decision.content_origin != "external":
        return ExternalDocumentMetadata(None, None, None)
    candidates: list[tuple[DocumentNumberMatch, dict[str, object]]] = []
    if match := _extract_current_document_number(item.title or ""):
        candidates.append((match, {"source": "oa_title", "oa_item_key": item.oa_item_key, "raw": match.raw, "normalized": match.normalized}))
    files = session.scalars(select(ArchivedFile).where(
        ArchivedFile.oa_item_id == item.id,
        ArchivedFile.download_status == "verified",
    ).order_by(ArchivedFile.id)).all()
    for source in files:
        if match := _extract_current_document_number(source.original_name or ""):
            candidates.append((match, {"source": "attachment_filename", "oa_item_key": item.oa_item_key, "source_file_id": source.id, "raw": match.raw, "normalized": match.normalized}))
        artifact = _valid_artifact(session, source)
        if artifact is None:
            continue
        product = resolve_cache_path(settings, artifact.output_relpath)
        if product.is_file() and (match := _extract_current_document_number(product.read_text(encoding="utf-8", errors="replace"))):
            candidates.append((match, {"source": "parsed_body", "oa_item_key": item.oa_item_key, "source_file_id": source.id, "parse_artifact_id": artifact.id, "raw": match.raw, "normalized": match.normalized}))
        elif product.is_file() and (sidecar := _sidecar_document_number(product)):
            match, page = sidecar
            candidates.append((match, {"source": "mineru_middle_json", "oa_item_key": item.oa_item_key, "source_file_id": source.id, "parse_artifact_id": artifact.id, "page": page, "raw": match.raw, "normalized": match.normalized}))
    values = {match.normalized for match, _ in candidates}
    if len(values) != 1:
        return ExternalDocumentMetadata(None, decision.canonical_issuer, None)
    number = next(iter(values))
    decision_number = _normalize_document_number(decision.document_number)
    if decision_number and decision_number != number:
        return ExternalDocumentMetadata(None, decision.canonical_issuer, None)
    # Prefer primary text proof over a filename/title candidate.
    evidence = next(
        (meta for _, meta in candidates if meta["source"] in {"parsed_body", "mineru_middle_json"}),
        candidates[0][1],
    )
    return ExternalDocumentMetadata(number, decision.canonical_issuer, evidence)


def _normalized_issuer(value: str | None) -> str | None:
    if not value:
        return None
    normalized = " ".join(value.split()).strip("：:;；，,。")
    return normalized or None


def require_current_classified_decision(session: Session, oa_item_key: str) -> ClassificationDecision:
    decision = session.scalar(select(ClassificationDecision).where(
        ClassificationDecision.oa_item_key == oa_item_key,
        ClassificationDecision.is_current.is_(True),
    ))
    if (
        decision is None
        or decision.classification_status != "classified"
        or decision.content_integrity_status not in {"ok", "no_attachment_confirmed"}
        or (decision.content_origin == "internal" and not decision.business_category)
        or (decision.content_origin == "external" and not decision.canonical_issuer)
        or decision.content_origin not in {"internal", "external"}
    ):
        raise ValueError("current classified decision is required for Markdown publication")
    return decision


def classify_done_item(session: Session, oa_item_key: str) -> OAItem:
    """Apply deterministic V2 classification without creating classification history."""
    item = session.scalar(select(OAItem).where(
        OAItem.oa_item_key == oa_item_key,
        OAItem.source_channel == "done",
    ))
    if item is None:
        raise LookupError("done item not found")

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
        item.source_type = "unclassified"
    item.classification_version = "v2-rules"
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
    decision = require_current_classified_decision(session, oa_item_key)
    external_metadata = resolve_external_document_metadata(session, settings, item, decision)
    if decision.content_origin == "external" and item.document_number != external_metadata.document_number:
        item.document_number = external_metadata.document_number
    destination = settings.markdown_root / _classification_directory(item) / _item_leaf(item) / "_index.md"
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

    document_number = external_metadata.document_number or (
        _normalize_document_number(item.document_number) if decision.content_origin != "external" else None
    )
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
        "document_number": document_number or item.document_number,
        "canonical_issuer": decision.canonical_issuer,
        "document_number_evidence": external_metadata.evidence,
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
            lines.append(f"- [{file.original_name}]({quote(relative, safe='/')})")
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


def _item_leaf(item: OAItem) -> str:
    """Readable title with a stable suffix to keep same-title OA items separate."""
    identity = sha256(item.oa_item_key.encode('utf-8')).hexdigest()[:10]
    number = _normalize_document_number(item.document_number) if item.source_type == "external" else None
    if item.source_type != "external":
        return f"{safe_filename(_display_title(item, None), 180)}__{identity}"
    title = _display_title(item, number)
    # A title's explicit leading number is display metadata, not a new decision.
    leading = _DOCUMENT_NUMBER.match(unicodedata.normalize("NFKC", title))
    if not number and leading:
        number = _normalize_document_number(leading.group("number"))
        title = title[leading.end():].lstrip(" -—_：:")
    base = safe_filename(f"{number} - {title}" if number else title, 240)
    session = object_session(item)
    if session is not None:
        parent = _classification_directory(item)
        rows = session.execute(select(MarkdownExport.oa_item_id, MarkdownExport.markdown_relpath)).all()
        own = {Path(path).parent.name for owner, path in rows if owner == item.id}
        if base + "__" + identity in own:
            return base + "__" + identity
        if any(owner != item.id and Path(path).parent == parent / base for owner, path in rows):
            return base + "__" + identity
    else:
        return base + "__" + identity
    return base


def _classification_directory(item: OAItem) -> Path:
    if item.source_type == "internal":
        category = _DECISION_CATEGORY_DIRECTORIES.get(
            item.internal_category or "", item.internal_category
        )
        if category in INTERNAL_CATEGORIES:
            return Path("内部") / category
    if item.source_type == "external" and item.external_issuer:
        return Path("外部") / safe_filename(item.external_issuer, 100)
    return Path("unclassified")
