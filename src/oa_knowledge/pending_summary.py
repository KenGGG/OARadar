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
from oa_knowledge.enrich.provider import make_llm_client
from oa_knowledge.enrich.context_budget import ContextBudget, chunk_text, discover_ollama_profile, estimate_tokens
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
    brief_content: str = ""
    confidence: float = Field(ge=0, le=1)


class PendingSummaryError(RuntimeError):
    pass


class PendingChunkSummary(BaseModel):
    summary: str = Field(min_length=1, max_length=600)
    evidence: list[str] = Field(default_factory=list, max_length=8)


def pending_evidence(payload: str, max_chars: int = 12_000) -> str:
    return payload[:max_chars]


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
            attachment_overview=[], brief_content=str(payload.get("brief_content") or ""), confidence=0.5,
        )


def normalize_pending_content(content: str | None) -> PendingSummary:
    parsed = validate_json_response(content)
    if isinstance(parsed, dict):
        try:
            return normalize_pending_response(parsed)
        except Exception as exc:
            fallback = next((
                parsed.get(key) for key in ("message", "content", "problem", "overview", "description", "result")
                if isinstance(parsed.get(key), str) and parsed.get(key).strip()
            ), None)
            if fallback:
                return PendingSummary(summary=fallback.strip()[:2000], confidence=0.25)
            raise PendingSummaryError("OLLAMA_SCHEMA_INVALID") from exc
    text = (content or "").strip()
    raise PendingSummaryError("OLLAMA_SCHEMA_INVALID")


def deterministic_pending_fallback(payload: str) -> PendingSummary:
    """Produce a clearly marked, non-semantic fallback from explicit fields."""
    try:
        raw = json.loads(payload)
        data = raw if isinstance(raw, dict) else {}
    except Exception:
        data = {}
    title = str(data.get("title") or data.get("subject") or "待办事项").strip()[:300]
    stage = str(data.get("current_node") or data.get("current_stage") or "").strip()[:200]
    sender = str(data.get("sender") or data.get("initiator") or "").strip()[:200]
    stage_text = f"，当前节点：{stage}" if stage else ""
    summary = f"【本地模型降级】{title}{stage_text}。模型多次失败，请登录OA核对原文和附件。"
    return PendingSummary(
        summary=summary,
        matter_type="未识别",
        initiator=sender,
        current_stage=stage,
        key_points=[],
        required_action="请登录OA核对并处理",
        amounts=[], deadlines=[], risks=[], attachment_overview=[],
        brief_content=summary[:200],
        confidence=0.0,
    )


def summarize_evidence(client, payload: str, *, max_input_tokens: int) -> PendingSummary:
    """Map/reduce long immutable evidence while bounding every user prompt."""
    if max_input_tokens < 512:
        raise PendingSummaryError("OLLAMA_CONTEXT_BUDGET_TOO_SMALL")
    map_prefix = "请概括这个有序证据分块，只保留有原文支持的事实：\n"
    chunk_budget = max_input_tokens - estimate_tokens(map_prefix)
    chunks = chunk_text(payload, max_tokens=chunk_budget)
    if not chunks:
        chunks = ["无正文证据"]

    chunk_schema = PendingChunkSummary.model_json_schema()
    map_system = (
        "你是本地OA证据分块提要器。严格输出分块提要JSON；不得编造。分块提要必须包含summary和evidence。"
        "这是分块提要，不是最终摘要。\n" + json.dumps(chunk_schema, ensure_ascii=False)
    )

    def map_chunks(parts: list[str]) -> list[str]:
        summaries: list[str] = []
        for part in parts:
            user = map_prefix + part
            if estimate_tokens(user) > max_input_tokens:
                raise PendingSummaryError("OLLAMA_CONTEXT_BUDGET_EXCEEDED")
            response = client.chat(map_system, user, json_schema=chunk_schema)
            if response.get("error"):
                raise PendingSummaryError(response.get("error"))
            parsed = validate_json_response(response.get("content"))
            try:
                summary = PendingChunkSummary.model_validate(parsed)
            except Exception as exc:
                raise PendingSummaryError("OLLAMA_CHUNK_SCHEMA_INVALID") from exc
            summaries.append(summary.model_dump_json())
        return summaries

    if len(chunks) > 1:
        reduced_parts = map_chunks(chunks)
        combined = "\n".join(reduced_parts)
        while estimate_tokens(combined) > max_input_tokens:
            groups = chunk_text(combined, max_tokens=chunk_budget)
            reduced_parts = map_chunks(groups)
            next_combined = "\n".join(reduced_parts)
            if len(next_combined) >= len(combined):
                raise PendingSummaryError("OLLAMA_REDUCE_DID_NOT_CONVERGE")
            combined = next_combined
        evidence = combined
    else:
        evidence = chunks[0]

    schema = PendingSummary.model_json_schema()
    final_system = (
        "你是本地OA待办概括器。只能依据输入，输出严格JSON；无证据的金额、日期、风险留空，不得编造。"
        "输出必须严格符合JSON Schema：\n" + json.dumps(schema, ensure_ascii=False) +
        "\nbrief_content不超过200字，并区分阅知类和办理/审批类事项。"
    )
    final_prefix = "请按约定字段概括以下不可变Pending证据或分块提要：\n"
    final_user = final_prefix + evidence
    if estimate_tokens(final_user) > max_input_tokens:
        raise PendingSummaryError("OLLAMA_CONTEXT_BUDGET_EXCEEDED")
    response = client.chat(final_system, final_user, json_schema=schema)
    if response.get("error"):
        raise PendingSummaryError(response.get("error"))
    return normalize_pending_content(response.get("content"))


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
        idem = f"pending:{logical_item_id}:{input_hash}:{settings.llm.model}:pending-v2"
        job = session.scalar(select(SummaryJob).where(SummaryJob.idempotency_key == idem))
        if job is None:
            job = SummaryJob(logical_item_id=logical_item_id, snapshot_id=snapshot.id, summary_kind="pending",
                             stage="item_summary", status="running", idempotency_key=idem,
                             max_attempts=settings.llm.max_retries + 1)
            session.add(job); session.flush()
        job.attempts += 1
        session.commit()
        job_id = job.id
        snapshot_id = snapshot.id
        attempt = job.attempts
        max_attempts = job.max_attempts

    if not settings.llm.enabled:
        summary = deterministic_pending_fallback(payload)
        result = {"model": "deterministic-fallback", "provider": "deterministic-fallback", "elapsed_seconds": None, "fallback": True}
    else:
        coordinator = ResourceCoordinator(engine)
        owner = f"worker-{os.getpid()}:pending-summary:{job_id}"
        lease = coordinator.acquire("local_llm", owner, ttl_seconds=settings.llm.timeout_seconds + 60,
                                    uses_local_gpu=settings.llm.uses_local_gpu)
        if lease is None:
            raise RuntimeError("GPU resource is busy")
        try:
            client = make_llm_client(settings.llm, max_retries=settings.llm.max_retries)
            profile = discover_ollama_profile(
                settings.llm.base_url, settings.llm.model,
                fallback_context_window=settings.llm.context_window_fallback,
                context_window_cap=settings.llm.context_window_cap,
            )
            budget = ContextBudget(
                context_window=profile.context_window, max_output_tokens=settings.llm.max_tokens,
                system_tokens=2000, safety_margin=settings.llm.context_safety_margin,
            )
            try:
                summary = summarize_evidence(client, payload, max_input_tokens=budget.max_input_tokens)
                result = {"model": settings.llm.model, "provider": settings.llm.provider_name, "elapsed_seconds": None, "fallback": False}
            except PendingSummaryError:
                if attempt < max_attempts:
                    raise
                summary = deterministic_pending_fallback(payload)
                result = {"model": "deterministic-fallback", "provider": "deterministic-fallback", "elapsed_seconds": None, "fallback": True}
        finally:
            coordinator.release(lease, owner)

    with Session(engine) as session:
        job = session.get(SummaryJob, job_id); assert job is not None
        version = (session.scalar(select(func.max(SummaryVersion.version)).where(
            SummaryVersion.logical_item_id == logical_item_id, SummaryVersion.summary_kind == "pending")) or 0) + 1
        row = SummaryVersion(logical_item_id=logical_item_id, snapshot_id=snapshot_id, summary_job_id=job.id,
                             summary_kind="pending", version=version, status="current",
                             input_hash=input_hash,
                             structured_json=summary.model_dump_json(), provider_name=result.get("provider") or settings.llm.provider_name,
                             model_name=result.get("model") or settings.llm.model,
                             prompt_version="pending-v2-fallback" if result.get("fallback") else "pending-v2",
                             elapsed_seconds=result.get("elapsed_seconds"), confidence=summary.confidence,
                             review_status="unreviewed", schema_valid=True)
        session.add(row); session.flush()
        logical = session.get(LogicalItem, logical_item_id)
        if logical: logical.current_pending_summary_id = row.id
        job.status = "completed"; session.commit(); session.refresh(row); session.expunge(row)
        return row
