"""Attachment-first knowledge vault trial publisher."""

from __future__ import annotations

import json
import re
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Callable

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.models import ArchivedFile, ContentObject, OAItem, ParseArtifact, ParseJob
from oa_knowledge.enrich.llm_client import LlmClient
from oa_knowledge.enrich.provider import make_llm_client


CATEGORIES = (
    "政府及上级文件",
    "上级控股集团文件",
    "上级金融集团文件",
    "本公司文件",
    "制度与内部流程",
    "项目与业务",
    "合同与法律",
    "决策与会议",
    "风险与租后",
    "财务审计预算",
)


class KnowledgeClassification(BaseModel):
    model_config = ConfigDict(extra="ignore")

    canonical_title: str = Field(min_length=1, max_length=300)
    primary_category: str
    organization_scope: str = Field(min_length=1, max_length=80)
    document_type: str = Field(min_length=1, max_length=80)
    business_domains: list[str] = Field(default_factory=list, max_length=8)
    projects: list[str] = Field(default_factory=list, max_length=8)
    knowledge_admission: bool
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("primary_category")
    @classmethod
    def known_category(cls, value: str) -> str:
        if value not in CATEGORIES and value != "待审核":
            raise ValueError("unknown primary category")
        return value


class ItemClassification(BaseModel):
    model_config = ConfigDict(extra="ignore")

    item_kind: str = Field(pattern="^(formal_document|project_topic|internal_operations|review)$")
    canonical_title: str = Field(min_length=1, max_length=300)
    document_number: str = Field(default="", max_length=80)
    organization_scope: str = Field(min_length=1, max_length=80)
    project_name: str = Field(default="", max_length=120)
    project_topic: str = Field(default="", max_length=120)
    internal_activity: str = Field(default="", max_length=120)
    importance: str = Field(pattern="^(high|medium|low)$")
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=500)


def item_destination(result: ItemClassification) -> Path:
    if result.item_kind == "formal_document":
        return Path("知识库/正式文件") / safe_name(result.organization_scope)
    if result.item_kind == "project_topic":
        return Path("知识库/项目专题") / safe_name(result.project_name or "待确认项目") / safe_name(result.project_topic or "综合资料")
    if result.item_kind == "internal_operations":
        return Path("知识库/内部运行资料") / safe_name(result.organization_scope) / safe_name(result.internal_activity or "日常运行")
    return Path("_待审核")


def render_source_record(
    *, oa_id: str, oa_title: str, classification_kind: str,
    knowledge_links: list[tuple[str, str]],
) -> tuple[Path, str]:
    relpath = Path("_来源记录/OA") / f"{safe_name(oa_id, 48)}__{safe_name(oa_title)}.md"
    lines = [
        f"# {oa_title}", "", f"OA事项ID：`{oa_id}`", "",
        f"事项分类：`{classification_kind}`", "", "## 知识文档", "",
    ]
    lines.extend(f"- [[{path[:-3]}|{label}]]" for path, label in knowledge_links)
    if not knowledge_links:
        lines.append("当前没有通过解析及发布门禁的附件文档。")
    return relpath, "\n".join(lines) + "\n"


def safe_name(value: str, limit: int = 120) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", value).strip(" ._")
    return (cleaned or "未命名")[:limit]


def _json_object(text: str) -> dict:
    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip(), flags=re.I)
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("model response contains no JSON object")


def parse_classification(text: str) -> KnowledgeClassification:
    try:
        return KnowledgeClassification.model_validate(_json_object(text))
    except (ValueError, ValidationError):
        return KnowledgeClassification(
            canonical_title="分类结果待审核",
            primary_category="待审核",
            organization_scope="其他",
            document_type="其他",
            knowledge_admission=False,
            confidence=0,
            reason="模型未返回符合约束的分类 JSON",
        )


def normalize_classification(
    result: KnowledgeClassification, filename: str
) -> KnowledgeClassification:
    """Keep one primary content directory while retaining organization as metadata."""
    text = f"{result.canonical_title} {filename} {result.document_type}"
    category = result.primary_category
    if any(word in text for word in ("风险", "租后", "逾期", "不良", "预警", "限制性重大事项")):
        category = "风险与租后"
    elif any(word in text for word in ("合同", "协议", "法律意见", "诉讼", "仲裁")):
        category = "合同与法律"
    elif any(word in text for word in ("制度", "办法", "规则", "细则", "规程")):
        category = "制度与内部流程"
    elif any(word in text for word in ("审计", "预算", "财务", "租金支付", "还款测算", "融资情况表")):
        category = "财务审计预算"
    elif any(word in text for word in ("会议纪要", "决议", "议案")):
        category = "决策与会议"
    title = result.canonical_title
    filename_stem = Path(filename).stem
    if Path(filename).suffix.lower() in {".xlsx", ".xls", ".csv"}:
        table_words = ("表", "清单", "台账")
        if any(word in filename_stem for word in table_words) and not any(word in title for word in table_words):
            title = filename_stem
    return result.model_copy(update={"primary_category": category, "canonical_title": title})


def classify_with_llm(
    client: LlmClient,
    *,
    oa_title: str,
    filename: str,
    parsed_text: str,
) -> tuple[KnowledgeClassification, dict]:
    system = (
        "你是本地 OA 附件知识分类器。OA 只是来源渠道，附件才是知识对象。"
        "只返回一个 JSON 对象，不要 Markdown。primary_category 只能是："
        + "、".join(CATEGORIES)
        + "。低价值报销、休假、普通付款凭证等 knowledge_admission=false；"
        "制度、合同、政府文件、项目评审、决议、会议纪要、风险租后、重要财务材料通常为 true。"
        "文件传阅的 OA 标题只是传递外壳，应根据附件正文识别正式标题和发文单位。"
    )
    user = json.dumps({
        "oa_title": oa_title,
        "attachment_filename": filename,
        "attachment_text": parsed_text[:4000],
        "required_fields": {
            "canonical_title": "正式文件标题",
            "primary_category": list(CATEGORIES),
            "organization_scope": "政府及上级/上级控股集团/上级金融集团/本公司/内部/其他",
            "document_type": "通知/制度/合同/报告/决议/纪要/表格/其他",
            "business_domains": ["最多8项"],
            "projects": ["最多8项"],
            "knowledge_admission": True,
            "confidence": 0.0,
            "reason": "简短依据",
        },
    }, ensure_ascii=False)
    response = client.chat(system, user)
    content = response.get("content") or ""
    result = parse_classification(content)
    if response.get("error"):
        result = result.model_copy(update={"reason": f"模型调用失败：{response['error']}"})
    return result, response


def _clean_oa_title(title: str) -> str:
    title = re.sub(r"^(?:【公告】|【通知】|【文件传阅】)+", "", title).strip()
    title = re.sub(r"\s*\(由[^()]{{}}]*原发\)\s*$", "", title).strip()
    return title


def parse_item_classification(text: str, oa_title: str) -> ItemClassification:
    try:
        result = ItemClassification.model_validate(_json_object(text))
    except (ValueError, ValidationError):
        result = ItemClassification(
            item_kind="review", canonical_title=_clean_oa_title(oa_title),
            organization_scope="待确认", importance="medium", confidence=0,
            reason="模型未返回符合约束的事项分类 JSON",
        )
    title = _clean_oa_title(oa_title)
    number_match = re.search(r"[\u4e00-\u9fff]{1,12}〔\d{4}〕\d+号", title)
    updates: dict[str, str] = {}
    if number_match and not result.document_number:
        updates["document_number"] = number_match.group(0)
    if number_match and result.item_kind == "review":
        updates.update(item_kind="formal_document", organization_scope="上级控股集团", importance="high")
    if "昊志机电" in title and "提前还款" in title:
        updates.update(
            item_kind="project_topic", project_name="昊志机电",
            project_topic="提前还款", organization_scope="本公司", importance="high",
        )
    if "债务合同限制性重大事项" in title:
        updates.update(
            item_kind="internal_operations", organization_scope="本公司",
            internal_activity="上报与统计", importance="low",
        )
    return result.model_copy(update=updates)


def classify_item_with_llm(
    client: LlmClient, *, oa_title: str, attachment_samples: list[dict]
) -> tuple[ItemClassification, dict]:
    system = (
        "你是本地 OA 知识库的事项级分类器。先判断整个 OA 事项的业务用途，不能逐附件决定顶层目录。"
        "只返回 JSON。item_kind 只能是 formal_document、project_topic、internal_operations、review。"
        "有正式文号且由政府、上级集团或公司发布的文件归 formal_document；"
        "围绕具体客户、承租人或交易项目的申请、合同、还款、投放资料归 project_topic，附件必须聚合在同一项目专题；"
        "印鉴、报送表、统计填报、普通审批等日常流程归 internal_operations，importance 通常 low；"
        "不能确定才归 review。"
    )
    user = json.dumps({
        "oa_title": oa_title,
        "attachments": attachment_samples,
        "required_fields": {
            "item_kind": "formal_document|project_topic|internal_operations|review",
            "canonical_title": "去除OA流转前后缀后的事项正式标题",
            "document_number": "正式文号，没有则为空",
            "organization_scope": "政府及上级单位/上级控股集团/上级金融集团/本公司/其他",
            "project_name": "项目主体，没有则为空",
            "project_topic": "具体业务专题，没有则为空",
            "internal_activity": "上报与统计/日常审批/其他，没有则为空",
            "importance": "high|medium|low", "confidence": 0.0, "reason": "依据",
        },
    }, ensure_ascii=False)
    response = client.chat(system, user)
    return parse_item_classification(response.get("content") or "", oa_title), response


def select_trial_item_ids(engine, limit: int) -> list[int]:
    """Select a stable sample without embedding environment-specific OA identifiers."""
    with Session(engine) as session:
        counts = (
            select(ArchivedFile.oa_item_id.label("item_id"), func.count(ArchivedFile.id).label("file_count"))
            .where(
                ArchivedFile.download_status == "verified",
                ArchivedFile.file_role.in_(("direct_attachment", "official_attachment")),
            )
            .group_by(ArchivedFile.oa_item_id)
            .subquery()
        )
        candidates = session.scalars(
            select(OAItem.id).join(counts, counts.c.item_id == OAItem.id)
            .where(counts.c.file_count <= 8)
            .order_by(OAItem.completed_at.desc(), OAItem.id.desc())
            .limit(max(0, limit))
        ).all()
        return list(candidates)


def prepare_trial_parses(settings: Settings, engine, item_ids: list[int]) -> dict:
    """Create valid parse artifacts for every parseable attachment in the sample."""
    from oa_knowledge.pipeline import ParsePipeline

    pipeline = ParsePipeline(settings, engine)
    with Session(engine) as session:
        file_ids = list(session.scalars(select(ArchivedFile.id).where(
            ArchivedFile.oa_item_id.in_(item_ids),
            ArchivedFile.download_status == "verified",
            ArchivedFile.file_role.in_(("direct_attachment", "official_attachment")),
        ).order_by(ArchivedFile.oa_item_id, ArchivedFile.id)).all())
    summary = {"files": len(file_ids), "completed": 0, "reused": 0, "failed": 0, "errors": []}
    for file_id in file_ids:
        try:
            job_id = pipeline.enqueue(file_id, engine="markitdown")
            if job_id is None:
                summary["failed"] += 1
                continue
            with Session(engine) as session:
                job = session.get(ParseJob, job_id)
                status = job.status if job else "missing"
            if status == "completed":
                summary["reused"] += 1
            elif status == "queued":
                pipeline.run(job_id)
                summary["completed"] += 1
            else:
                summary["failed"] += 1
                summary["errors"].append({"file_id": file_id, "status": status})
        except Exception as exc:
            summary["failed"] += 1
            summary["errors"].append({"file_id": file_id, "error": type(exc).__name__})
    return summary


def run_item_first_trial(
    settings: Settings, engine, *, item_ids: list[int], clear: bool = False,
) -> dict:
    vault = settings.data_root / "vault"
    if clear and vault.exists():
        shutil.rmtree(vault)
    vault.mkdir(parents=True, exist_ok=True)
    client = make_llm_client(
        settings.llm, max_retries=settings.llm.max_retries,
        max_tokens=min(settings.llm.max_tokens, 1200),
    )
    model_available = settings.llm.enabled and client.is_available()
    results: list[dict] = []

    with Session(engine) as session:
        items = list(session.scalars(select(OAItem).where(OAItem.id.in_(item_ids))).all())
        by_id = {item.id: item for item in items}
        for item_id in item_ids:
            item = by_id.get(item_id)
            if item is None:
                continue
            rows = session.execute(
                select(ArchivedFile, ContentObject, ParseArtifact)
                .join(ContentObject, ArchivedFile.content_object_id == ContentObject.id)
                .join(ParseArtifact, ContentObject.active_parse_artifact_id == ParseArtifact.id)
                .where(
                    ArchivedFile.oa_item_id == item.id,
                    ArchivedFile.download_status == "verified",
                    ArchivedFile.file_role.in_(("direct_attachment", "official_attachment")),
                    ParseArtifact.lifecycle_status == "valid",
                ).order_by(ArchivedFile.id)
            ).all()
            samples: list[dict] = []
            parsed_rows: list[tuple] = []
            seen: set[str] = set()
            for file, content, artifact in rows:
                if content.sha256 in seen:
                    continue
                seen.add(content.sha256)
                parsed_path = settings.data_root / "parse" / artifact.output_relpath
                if not parsed_path.is_file():
                    continue
                body = parsed_path.read_text(encoding="utf-8", errors="replace")
                samples.append({"filename": file.original_name, "text_sample": body[:700]})
                parsed_rows.append((file, content, artifact, body))
            if model_available:
                classification, metrics = classify_item_with_llm(
                    client, oa_title=item.title, attachment_samples=samples[:8]
                )
            else:
                classification = parse_item_classification("", item.title)
                metrics = {"model": settings.llm.model, "elapsed_seconds": 0, "error": "model_unavailable"}
            destination = item_destination(classification)
            oa_id = item.workitem_id_text or item.oa_item_key
            links: list[tuple[str, str]] = []
            used_names: set[str] = set()
            for index, (file, content, artifact, body) in enumerate(parsed_rows, 1):
                if classification.item_kind == "formal_document" and len(parsed_rows) == 1:
                    title = classification.canonical_title
                    number = classification.document_number.strip()
                    if number and number not in title:
                        title = f"{number} {title}"
                else:
                    title = Path(file.original_name).stem
                filename = f"KD-{content.id}__{safe_name(title)}.md"
                if filename in used_names:
                    filename = f"KD-{content.id}-{index}__{safe_name(title)}.md"
                used_names.add(filename)
                relpath = destination / filename
                knowledge_classification = KnowledgeClassification(
                    canonical_title=title,
                    primary_category=(
                        "项目与业务" if classification.item_kind == "project_topic"
                        else "本公司文件" if classification.item_kind == "internal_operations"
                        else "上级控股集团文件" if classification.organization_scope == "上级控股集团"
                        else "政府及上级文件"
                    ),
                    organization_scope=classification.organization_scope,
                    document_type="附件来源文档", business_domains=[],
                    projects=[classification.project_name] if classification.project_name else [],
                    knowledge_admission=classification.item_kind != "review",
                    confidence=classification.confidence, reason=classification.reason,
                )
                note = render_knowledge_note(
                    knowledge_id=f"KD-{content.id}", classification=knowledge_classification,
                    source_oa_id=oa_id, source_oa_title=item.title,
                    source_filename=file.original_name, content_sha256=content.sha256,
                    parse_artifact_id=artifact.id, parse_engine=artifact.engine, body=body,
                    model_name=str(metrics.get("model") or settings.llm.model),
                )
                target = vault / relpath
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(note, encoding="utf-8")
                links.append((relpath.as_posix(), title))

            if classification.item_kind == "project_topic" and links:
                overview_name = f"{safe_name(classification.project_topic or classification.canonical_title)}项目总览.md"
                overview_relpath = destination / overview_name
                overview_lines = [
                    "---", f"title: {classification.canonical_title}", "note_type: project_overview",
                    f"project: {classification.project_name}", f"project_topic: {classification.project_topic}",
                    f"source_oa_id: '{oa_id}'", "---", "", f"# {classification.canonical_title}", "", "## 项目资料", "",
                ]
                overview_lines.extend(f"- [[{path}|{label}]]" for path, label in links)
                overview_target = vault / overview_relpath
                overview_target.parent.mkdir(parents=True, exist_ok=True)
                overview_target.write_text("\n".join(overview_lines) + "\n", encoding="utf-8")
                links.insert(0, (overview_relpath.as_posix(), classification.canonical_title))

            source_relpath, source_text = render_source_record(
                oa_id=oa_id, oa_title=item.title,
                classification_kind=classification.item_kind, knowledge_links=links,
            )
            source_target = vault / source_relpath
            source_target.parent.mkdir(parents=True, exist_ok=True)
            source_target.write_text(source_text, encoding="utf-8")
            results.append({
                "oa_id": oa_id, "oa_title": item.title,
                "item_kind": classification.item_kind,
                "organization_scope": classification.organization_scope,
                "project_name": classification.project_name,
                "project_topic": classification.project_topic,
                "internal_activity": classification.internal_activity,
                "importance": classification.importance,
                "confidence": classification.confidence,
                "parsed_attachments": len(parsed_rows),
                "published_documents": len(links),
                "destination": destination.as_posix(),
                "elapsed_seconds": metrics.get("elapsed_seconds", 0),
                "model_error": metrics.get("error"),
            })

    report = {
        "mode": "item_first_trial", "model": settings.llm.model,
        "requested_items": len(item_ids), "processed_items": len(results),
        "published_documents": sum(row["published_documents"] for row in results),
        "review_items": sum(row["item_kind"] == "review" for row in results),
        "items": results,
    }
    (vault / "试运行报告.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _frontmatter(payload: dict) -> str:
    return "---\n" + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).rstrip() + "\n---\n"


def render_knowledge_note(
    *,
    knowledge_id: str,
    classification: KnowledgeClassification,
    source_oa_id: str,
    source_oa_title: str,
    source_filename: str,
    content_sha256: str,
    parse_artifact_id: int,
    parse_engine: str,
    body: str,
    model_name: str,
) -> str:
    metadata = {
        "id": knowledge_id,
        "title": classification.canonical_title,
        "note_type": "knowledge_document",
        "primary_category": classification.primary_category,
        "organization_scope": classification.organization_scope,
        "document_type": classification.document_type,
        "business_domains": classification.business_domains,
        "projects": classification.projects,
        "knowledge_admission": classification.knowledge_admission,
        "classification_confidence": classification.confidence,
        "classification_reason": classification.reason,
        "classification_model": model_name,
        "source_oa_ids": [source_oa_id],
        "source_oa_titles": [source_oa_title],
        "source_attachment_names": [source_filename],
        "content_sha256": content_sha256,
        "parse_artifact_id": parse_artifact_id,
        "parse_engine": parse_engine,
        "tags": ["oa/attachment", f"knowledge/{classification.primary_category}"],
    }
    return _frontmatter(metadata) + f"\n# {classification.canonical_title}\n\n{body.strip()}\n"


def _fallback_classification(filename: str) -> KnowledgeClassification:
    category = "项目与业务"
    document_type = "其他"
    if any(word in filename for word in ("合同", "协议", "法律")):
        category, document_type = "合同与法律", "合同"
    elif any(word in filename for word in ("制度", "办法", "规则", "细则")):
        category, document_type = "制度与内部流程", "制度"
    elif any(word in filename for word in ("会议纪要", "决议", "议案")):
        category, document_type = "决策与会议", "决策文件"
    elif any(word in filename for word in ("审计", "预算", "财务", "测算表")):
        category, document_type = "财务审计预算", "财务材料"
    return KnowledgeClassification(
        canonical_title=Path(filename).stem,
        primary_category=category,
        organization_scope="待模型确认",
        document_type=document_type,
        business_domains=[],
        projects=[],
        knowledge_admission=True,
        confidence=0.45,
        reason="模型不可用时的文件名兜底分类，需人工复核",
    )


def run_attachment_first_trial(
    settings: Settings,
    engine,
    *,
    limit: int = 10,
    clear: bool = False,
    classifier: Callable[..., tuple[KnowledgeClassification, dict]] | None = None,
) -> dict:
    vault = settings.data_root / "vault"
    if clear and vault.exists():
        shutil.rmtree(vault)
    vault.mkdir(parents=True, exist_ok=True)

    client = make_llm_client(
        settings.llm,
        max_retries=settings.llm.max_retries,
        # qwen3.5 emits its internal reasoning separately before JSON content.
        # A small cap can consume the whole response before the final object.
        max_tokens=min(settings.llm.max_tokens, 1800),
    )
    model_available = settings.llm.enabled and client.is_available()
    classify = classifier or classify_with_llm
    rows_out: list[dict] = []
    source_links: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)

    with Session(engine) as session:
        rows = session.execute(
            select(ArchivedFile, OAItem, ContentObject, ParseArtifact)
            .join(OAItem, ArchivedFile.oa_item_id == OAItem.id)
            .join(ContentObject, ArchivedFile.content_object_id == ContentObject.id)
            .join(ParseArtifact, ContentObject.active_parse_artifact_id == ParseArtifact.id)
            .where(
                ArchivedFile.download_status == "verified",
                ArchivedFile.file_role.in_(("direct_attachment", "official_attachment")),
                ParseArtifact.lifecycle_status == "valid",
            )
            .order_by(ParseArtifact.id, ArchivedFile.id)
            .limit(limit)
        ).all()

        seen_hashes: set[str] = set()
        for file, item, content, artifact in rows:
            if content.sha256 in seen_hashes:
                continue
            seen_hashes.add(content.sha256)
            parsed_path = settings.data_root / "parse" / artifact.output_relpath
            if not parsed_path.is_file():
                continue
            body = parsed_path.read_text(encoding="utf-8", errors="replace")
            if model_available:
                classification, metrics = classify(
                    client, oa_title=item.title, filename=file.original_name, parsed_text=body
                )
            else:
                classification, metrics = _fallback_classification(file.original_name), {
                    "model": settings.llm.model, "elapsed_seconds": 0, "error": "model_unavailable"
                }
            classification = normalize_classification(classification, file.original_name)
            review = (
                not classification.knowledge_admission
                or classification.confidence < 0.7
                or classification.primary_category == "待审核"
            )
            directory = "_待审核" if review else f"知识库/{classification.primary_category}"
            title = classification.canonical_title
            if title == "分类结果待审核":
                title = Path(file.original_name).stem
                classification = classification.model_copy(update={"canonical_title": title})
            knowledge_id = f"KD-{content.id}"
            relpath = Path(directory) / f"{knowledge_id}__{safe_name(title)}.md"
            oa_id = item.workitem_id_text or item.oa_item_key
            note = render_knowledge_note(
                knowledge_id=knowledge_id,
                classification=classification,
                source_oa_id=oa_id,
                source_oa_title=item.title,
                source_filename=file.original_name,
                content_sha256=content.sha256,
                parse_artifact_id=artifact.id,
                parse_engine=artifact.engine,
                body=body,
                model_name=str(metrics.get("model") or settings.llm.model),
            )
            target = vault / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(note, encoding="utf-8")
            source_links[(oa_id, item.title)].append((relpath.as_posix(), title))
            rows_out.append({
                "knowledge_id": knowledge_id,
                "source_oa_id": oa_id,
                "source_filename": file.original_name,
                "category": classification.primary_category,
                "admitted": classification.knowledge_admission,
                "confidence": classification.confidence,
                "review": review,
                "vault_relpath": relpath.as_posix(),
                "elapsed_seconds": metrics.get("elapsed_seconds", 0),
                "model_error": metrics.get("error"),
            })

    for (oa_id, title), links in source_links.items():
        source_dir = vault / "_来源记录" / "OA" / f"{safe_name(oa_id, 48)}__{safe_name(title)}"
        source_dir.mkdir(parents=True, exist_ok=True)
        lines = [f"# {title}", "", f"OA事项ID：`{oa_id}`", "", "## 附件知识文档", ""]
        lines.extend(f"- [[{path[:-3]}|{label}]]" for path, label in links)
        source_dir.joinpath("来源记录.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    manifest = {
        "mode": "attachment_first_trial",
        "model": settings.llm.model,
        "model_available": model_available,
        "requested_limit": limit,
        "published": len(rows_out),
        "admitted": sum(not row["review"] for row in rows_out),
        "review": sum(row["review"] for row in rows_out),
        "items": rows_out,
    }
    (vault / "试运行报告.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
