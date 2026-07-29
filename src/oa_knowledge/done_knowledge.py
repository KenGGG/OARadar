"""Evidence-gated local knowledge extraction and incremental Vault publication."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oa_knowledge.archive import atomic_write_bytes
from oa_knowledge.config import Settings
from oa_knowledge.db.models import ArchivedFile, ContentObject, ItemSnapshot, LogicalItem, OAItem, ParseArtifact, SummaryJob, SummaryVersion
from oa_knowledge.enrich.extractor import validate_json_response
from oa_knowledge.enrich.llm_client import LlmClient
from oa_knowledge.lifecycle import record_snapshot
from oa_knowledge.resources import ResourceCoordinator


class EvidenceStatement(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    evidence: str = Field(min_length=1, max_length=300)


class DoneKnowledge(BaseModel):
    problem: str = Field(max_length=1000)
    core_conclusions: list[EvidenceStatement] = Field(default_factory=list, max_length=3)
    business_data: list[EvidenceStatement] = Field(default_factory=list, max_length=3)
    approval_conditions: list[EvidenceStatement] = Field(default_factory=list, max_length=3)
    risks: list[EvidenceStatement] = Field(default_factory=list, max_length=3)
    actions: list[EvidenceStatement] = Field(default_factory=list, max_length=3)
    reusable_knowledge: list[EvidenceStatement] = Field(default_factory=list, max_length=3)
    confidence: float = Field(ge=0, le=1)


class DoneOverview(BaseModel):
    problem: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(ge=0, le=1)


def done_generation_schema() -> dict:
    return DoneOverview.model_json_schema()


class DoneKnowledgeError(RuntimeError):
    pass


class NoAttachmentEvidence(LookupError):
    pass


def retry_evidence(source: str, attempt: int) -> str:
    budget = max(2000, 8000 // max(1, attempt))
    return source[:budget]


def done_max_tokens(configured: int) -> int:
    return min(configured, 600)


def normalize_done_response(payload: dict) -> DoneKnowledge:
    try:
        return DoneKnowledge.model_validate(payload)
    except Exception:
        problem = None
        if isinstance(payload, dict):
            problem = payload.get("problem") or payload.get("summary") or payload.get("overview") or payload.get("content")
            issue_info = payload.get("issue_info")
            if not problem and isinstance(issue_info, dict):
                problem = issue_info.get("summary") or issue_info.get("overview") or issue_info.get("content") or issue_info.get("purpose")
            sections = payload.get("sections")
            if not problem and isinstance(sections, list):
                problem = next(
                    (section.get("content") or section.get("summary") for section in sections if isinstance(section, dict) and (section.get("content") or section.get("summary"))),
                    None,
                )
        if not isinstance(problem, str) or not problem.strip():
            raise
        confidence = payload.get("confidence", 0.5)
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            confidence = 0.5
        return DoneKnowledge(problem=problem.strip()[:1000], confidence=float(confidence))


def find_vault_overview(root: Path, oa_item_key: str) -> Path | None:
    expected = oa_item_key.removeprefix("done:").removeprefix("pending:")
    for path in root.rglob("OA-*__事项总览.md"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if not text.startswith("---\n"):
            continue
        end = text.find("\n---\n", 4)
        if end < 0:
            continue
        metadata = yaml.safe_load(text[4:end]) or {}
        actual = str(metadata.get("oa_item_id"))
        if actual == oa_item_key or actual == expected:
            return path
    return None


def build_attachment_evidence(
    artifacts: list[tuple[int, str, str]], *, total_budget: int = 6000,
) -> tuple[str, list[int]]:
    if not artifacts:
        return "", []
    per_attachment = max(100, total_budget // len(artifacts))
    chunks, ids = [], []
    for artifact_id, filename, text in artifacts:
        sample = text[:per_attachment]
        if not sample.strip():
            continue
        digest = hashlib.sha256(sample.encode()).hexdigest()
        chunks.append(
            f"[parse_artifact:{artifact_id} file:{filename} block:head sha256:{digest}]\n{sample}"
        )
        ids.append(artifact_id)
    return "\n\n".join(chunks), ids


def _source_text(settings: Settings, session: Session, item: OAItem) -> tuple[str, list[int]]:
    rows = session.execute(select(ParseArtifact, ArchivedFile.original_name).join(ContentObject, ContentObject.id == ParseArtifact.content_object_id)
        .join(ArchivedFile, ArchivedFile.content_object_id == ContentObject.id)
        .where(
            ArchivedFile.oa_item_id == item.id,
            ParseArtifact.lifecycle_status == "valid",
            ArchivedFile.file_role.in_(("direct_attachment", "official_body", "official_attachment", "associated_document", "opinion_attachment")),
        )
        .order_by(ParseArtifact.id)).all()
    samples: list[tuple[int, str, str]] = []
    seen: set[int] = set()
    for artifact, filename in rows:
        if artifact.id in seen:
            continue
        path = settings.data_root / "parse" / artifact.output_relpath
        if not path.is_file():
            continue
        samples.append((artifact.id, filename or f"attachment-{artifact.id}", path.read_text(encoding="utf-8", errors="replace")))
        seen.add(artifact.id)
    return build_attachment_evidence(samples)


def generate_done_knowledge(settings: Settings, engine, oa_item_key: str) -> SummaryVersion:
    with Session(engine) as session:
        item = session.scalar(select(OAItem).where(OAItem.oa_item_key == oa_item_key))
        if item is None: raise LookupError("done OA item not found")
        source, artifact_ids = _source_text(settings, session, item)
        if not source: raise NoAttachmentEvidence("no valid attachment parse evidence")
        logical = session.get(LogicalItem, item.logical_item_id) if item.logical_item_id else None
        if logical is None:
            logical = LogicalItem(logical_key=f"done:{item.workitem_id_text or item.oa_item_key}", title=item.title, lifecycle_status="done")
            session.add(logical); session.flush(); item.logical_item_id = logical.id
        snapshot = record_snapshot(session, logical.id, None, "done_final", {
            "oa_item_key": item.oa_item_key, "title": item.title, "completed_at": item.completed_at.isoformat() if item.completed_at else None,
            "parse_artifact_ids": artifact_ids,
        }, is_canonical=True)
        input_hash = hashlib.sha256((snapshot.content_hash + source).encode()).hexdigest()
        existing = session.scalar(select(SummaryVersion).where(SummaryVersion.logical_item_id == logical.id,
            SummaryVersion.summary_kind == "done", SummaryVersion.input_hash == input_hash, SummaryVersion.status == "current"))
        if existing:
            knowledge = DoneKnowledge.model_validate_json(existing.structured_json)
            _publish(settings, oa_item_key, item.title, existing, knowledge, artifact_ids)
            return existing
        idem = f"done:{logical.id}:{input_hash}:{settings.llm.model}:knowledge-v1"
        job = session.scalar(select(SummaryJob).where(SummaryJob.idempotency_key == idem))
        if job is None:
            job = SummaryJob(logical_item_id=logical.id, snapshot_id=snapshot.id, summary_kind="done", stage="item_summary",
                             status="running", idempotency_key=idem, max_attempts=settings.llm.max_retries + 1)
            session.add(job); session.flush()
        job.attempts += 1; session.commit(); logical_id=logical.id; snapshot_id=snapshot.id; job_id=job.id; title=item.title; attempt=job.attempts
    coordinator=ResourceCoordinator(engine); owner=f"worker-{os.getpid()}:done-knowledge:{job_id}"
    lease=coordinator.acquire("local_llm",owner,ttl_seconds=settings.llm.timeout_seconds+60,uses_local_gpu=settings.llm.uses_local_gpu)
    if lease is None: raise RuntimeError("GPU resource is busy")
    try:
        schema=json.dumps(done_generation_schema(),ensure_ascii=False)
        result=LlmClient(base_url=settings.llm.base_url,api_key_env=settings.llm.api_key_env,model=settings.llm.model,
                         temperature=settings.llm.temperature,max_tokens=done_max_tokens(settings.llm.max_tokens),
                         timeout_seconds=settings.llm.timeout_seconds,max_retries=settings.llm.max_retries,
                         provider_mode=settings.llm.provider_mode).chat(
            "你是本地OA附件概括器。严格输出简洁JSON，不要解释。problem用中文概括附件的主题、主要事项和要求；confidence为0到1。输出必须符合JSON Schema：\n"+schema,
            f"标题：{title}\n\n附件头部：\n{retry_evidence(source, attempt)}", json_schema=done_generation_schema())
        parsed=validate_json_response(result.get("content"))
        if result.get("error") or parsed is None: raise DoneKnowledgeError(result.get("error") or "OLLAMA_SCHEMA_INVALID")
        knowledge=normalize_done_response(parsed)
    finally: coordinator.release(lease,owner)
    with Session(engine) as session:
        version=(session.scalar(select(func.max(SummaryVersion.version)).where(SummaryVersion.logical_item_id==logical_id,SummaryVersion.summary_kind=="done")) or 0)+1
        row=SummaryVersion(logical_item_id=logical_id,snapshot_id=snapshot_id,summary_job_id=job_id,summary_kind="done",version=version,
            status="current",input_hash=input_hash,structured_json=knowledge.model_dump_json(),provider_name=settings.llm.provider_name,
            model_name=result.get("model") or settings.llm.model,prompt_version="knowledge-v1",elapsed_seconds=result.get("elapsed_seconds"),
            confidence=knowledge.confidence,review_status="unreviewed",schema_valid=True)
        session.add(row); session.flush(); logical=session.get(LogicalItem,logical_id); logical.current_done_summary_id=row.id
        job=session.get(SummaryJob,job_id); job.status="completed"; session.commit(); session.refresh(row); session.expunge(row)
    _publish(settings, oa_item_key, title, row, knowledge, artifact_ids)
    return row


def _publish(settings: Settings, oa_item_key: str, title: str, version: SummaryVersion, knowledge: DoneKnowledge, artifact_ids: list[int]) -> None:
    root=settings.data_root/"vault"
    overview=find_vault_overview(root, oa_item_key)
    if overview is None: raise LookupError("Vault OA folder not found")
    name=overview.name.replace("__事项总览.md","__知识提炼.md"); destination=overview.parent/name
    sections=[("一、事项解决的问题",knowledge.problem),("二、核心结论",knowledge.core_conclusions),("三、关键业务数据",knowledge.business_data),
              ("四、审批条件和执行要求",knowledge.approval_conditions),("五、风险及控制措施",knowledge.risks),
              ("七、可复用知识",knowledge.reusable_knowledge),("八、待办、期限和责任人",knowledge.actions)]
    lines=["---",yaml.safe_dump({"managed_by":"oaradar","doc_kind":"knowledge_extraction","summary_version_id":version.id,
        "model":version.model_name,"prompt_version":version.prompt_version,"parse_artifact_ids":artifact_ids},allow_unicode=True,sort_keys=False).rstrip(),"---",f"# {title}：知识提炼",""]
    for heading,value in sections:
        lines += [f"## {heading}",""]
        if isinstance(value,str): lines += [value or "无可验证结论。",""]
        else: lines += [*(f"- {x.text}（证据：`{x.evidence}`）" for x in value),""] if value else ["无可验证结论。",""]
    lines += ["## 十、来源证据","",*(f"- ParseArtifact #{x}" for x in artifact_ids),""]
    rel=destination.relative_to(settings.data_root)
    atomic_write_bytes("\n".join(lines).encode("utf-8"),settings.data_root,rel)
