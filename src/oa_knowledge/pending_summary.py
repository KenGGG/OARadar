"""Evidence-constrained local Ollama summaries for immutable Pending snapshots."""

from __future__ import annotations

import hashlib
import json
import os

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.models import ContentObject, ItemSnapshot, LogicalItem, ParseArtifact, SourceAttachment, SummaryJob, SummaryVersion
from oa_knowledge.enrich.extractor import validate_json_response
from oa_knowledge.enrich.llm_client import LlmClient
from oa_knowledge.resources import ResourceCoordinator


class Amount(BaseModel):
    name: str = ""; value: str = ""; currency: str = "CNY"; evidence: str = ""


class Deadline(BaseModel):
    date: str = ""; description: str = ""; evidence: str = ""


class Risk(BaseModel):
    risk: str = ""; evidence: str = ""


class AttachmentOverview(BaseModel):
    filename: str; likely_role: str = ""


class PendingSummary(BaseModel):
    summary: str
    matter_type: str = ""
    initiator: str = ""
    current_stage: str = ""
    key_points: list[str] = Field(default_factory=list)
    required_action: str = ""
    amounts: list[Amount] = Field(default_factory=list)
    deadlines: list[Deadline] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    attachment_overview: list[AttachmentOverview] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class PendingSummaryError(RuntimeError):
    pass


def normalize_pending_response(payload: dict) -> PendingSummary:
    """Accept the strict schema or a conservative summary-only model fallback."""
    try:
        return PendingSummary.model_validate(payload)
    except Exception:
        summary = payload.get("summary") if isinstance(payload, dict) else None
        if not isinstance(summary, str) or not summary.strip():
            raise
        return PendingSummary(
            summary=summary.strip(), matter_type=str(payload.get("matter_type") or ""),
            initiator=str(payload.get("initiator") or ""), current_stage=str(payload.get("current_stage") or ""),
            key_points=[str(x) for x in payload.get("key_points", []) if isinstance(x, str)],
            required_action=str(payload.get("required_action") or ""), amounts=[], deadlines=[], risks=[],
            attachment_overview=[], confidence=0.5,
        )


def normalize_pending_content(content: str | None) -> PendingSummary:
    parsed = validate_json_response(content)
    if isinstance(parsed, dict):
        try:
            return normalize_pending_response(parsed)
        except Exception as exc:
            raise PendingSummaryError("OLLAMA_SCHEMA_INVALID") from exc
    text = (content or "").strip()
    if not text:
        raise PendingSummaryError("OLLAMA_SCHEMA_INVALID")
    return PendingSummary(summary=text[:2000], confidence=0.25)


def summarize_pending(settings: Settings, engine, logical_item_id: int) -> SummaryVersion:
    with Session(engine) as session:
        snapshot = session.scalar(select(ItemSnapshot).where(
            ItemSnapshot.logical_item_id == logical_item_id,
            ItemSnapshot.snapshot_kind.in_(("pending_initial", "pending_updated")),
        ).order_by(ItemSnapshot.id.desc()).limit(1))
        if snapshot is None:
            raise LookupError("pending snapshot not found")
        payload = snapshot.payload_json
        artifact_paths = session.scalars(
            select(ParseArtifact.output_relpath)
            .join(ContentObject, ContentObject.active_parse_artifact_id == ParseArtifact.id)
            .join(SourceAttachment, SourceAttachment.content_object_id == ContentObject.id)
            .where(
                SourceAttachment.snapshot_id == snapshot.id,
                ParseArtifact.lifecycle_status == "valid",
            )
            .order_by(SourceAttachment.ordinal)
        ).all()
        markdown_parts = []
        for relpath in artifact_paths:
            path = settings.data_root / relpath
            if path.is_file():
                markdown_parts.append(path.read_text(encoding="utf-8", errors="replace"))
        if markdown_parts:
            payload += "\n\n附件 Markdown：\n" + "\n\n---\n\n".join(markdown_parts)
        input_hash = hashlib.sha256(payload.encode()).hexdigest()
        existing = session.scalar(select(SummaryVersion).where(
            SummaryVersion.logical_item_id == logical_item_id, SummaryVersion.summary_kind == "pending",
            SummaryVersion.input_hash == input_hash, SummaryVersion.status == "current",
        ))
        if existing:
            return existing
        idem = f"pending:{logical_item_id}:{input_hash}:{settings.llm.model}:pending-v1"
        job = session.scalar(select(SummaryJob).where(SummaryJob.idempotency_key == idem))
        if job is None:
            job = SummaryJob(logical_item_id=logical_item_id, snapshot_id=snapshot.id, summary_kind="pending",
                             stage="item_summary", status="running", idempotency_key=idem,
                             max_attempts=settings.llm.max_retries + 1)
            session.add(job); session.flush()
        job.attempts += 1; session.commit(); job_id = job.id; snapshot_id = snapshot.id

    coordinator = ResourceCoordinator(engine)
    owner = f"worker-{os.getpid()}:pending-summary:{job_id}"
    lease = coordinator.acquire("local_llm", owner, ttl_seconds=settings.llm.timeout_seconds + 60,
                                uses_local_gpu=settings.llm.uses_local_gpu)
    if lease is None:
        raise RuntimeError("GPU resource is busy")
    try:
        client = LlmClient(base_url=settings.llm.base_url, api_key_env=settings.llm.api_key_env,
                           model=settings.llm.model, temperature=settings.llm.temperature,
                           max_tokens=settings.llm.max_tokens, timeout_seconds=settings.llm.timeout_seconds,
                           max_retries=settings.llm.max_retries, provider_mode=settings.llm.provider_mode)
        system_prompt = ("你是本地OA待办概括器。只能依据输入，输出严格JSON；无证据的金额、日期、风险留空，不得编造。"
                         "输出必须严格符合以下JSON Schema，不得增删顶层字段：\n" +
                         json.dumps(PendingSummary.model_json_schema(), ensure_ascii=False))
        result = client.chat(system_prompt, "请按约定字段概括以下不可变Pending快照：\n" + payload[:60000])
        if result.get("error"):
            raise PendingSummaryError(result.get("error"))
        summary = normalize_pending_content(result.get("content"))
    finally:
        coordinator.release(lease, owner)

    with Session(engine) as session:
        job = session.get(SummaryJob, job_id); assert job is not None
        version = (session.scalar(select(func.max(SummaryVersion.version)).where(
            SummaryVersion.logical_item_id == logical_item_id, SummaryVersion.summary_kind == "pending")) or 0) + 1
        row = SummaryVersion(logical_item_id=logical_item_id, snapshot_id=snapshot_id, summary_job_id=job.id,
                             summary_kind="pending", version=version, status="current",
                             input_hash=input_hash,
                             structured_json=summary.model_dump_json(), provider_name=settings.llm.provider_name,
                             model_name=result.get("model") or settings.llm.model, prompt_version="pending-v1",
                             elapsed_seconds=result.get("elapsed_seconds"), confidence=summary.confidence,
                             review_status="unreviewed", schema_valid=True)
        session.add(row); session.flush()
        logical = session.get(LogicalItem, logical_item_id)
        if logical: logical.current_pending_summary_id = row.id
        job.status = "completed"; session.commit(); session.refresh(row); session.expunge(row)
        return row
