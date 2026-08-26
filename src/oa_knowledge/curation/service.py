"""Incremental orchestration for local curated knowledge."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.curation.canonical import canonical_key
from oa_knowledge.curation.classifier import PROMPT_VERSION, classify_package
from oa_knowledge.curation.package import OAPackage, build_package, package_signature
from oa_knowledge.curation.publisher import publish_document, remove_managed_publication, validate_publication
from oa_knowledge.curation.rules import RULES_VERSION, SourceDisposition, classify_source
from oa_knowledge.curation.schemas import SCHEMA_VERSION
from oa_knowledge.db.models import (
    ArchivedFile, CuratedDecision, CuratedDecisionSource, CuratedRun, KnowledgeDocument,
    LogicalItem, OAItem, OAItemDocumentRelation, SourceReference,
)
from oa_knowledge.enrich.context_budget import ContextBudget, discover_ollama_profile
from oa_knowledge.enrich.provider import make_llm_client
from oa_knowledge.resources import ResourceCoordinator


@dataclass(frozen=True)
class CurationStats:
    packages: int = 0
    sources: int = 0
    noise_sources: int = 0
    candidate_sources: int = 0
    skipped: int = 0
    completed: int = 0
    needs_review: int = 0
    failed: int = 0

    def model_dump(self) -> dict:
        return asdict(self)


def _config_signature(settings: Settings) -> str:
    payload = {
        "confidence_threshold": settings.curation.confidence_threshold,
        "max_input_tokens": settings.curation.max_input_tokens,
        "context_window_cap": settings.llm.context_window_cap,
        "context_safety_margin": settings.llm.context_safety_margin,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _candidate_items(session: Session, *, oa_item_key: str | None, limit: int) -> list[OAItem]:
    query = select(OAItem).join(ArchivedFile, ArchivedFile.oa_item_id == OAItem.id)
    if oa_item_key:
        query = query.where(OAItem.oa_item_key == oa_item_key)
    rows = list(session.scalars(query.order_by(OAItem.id).distinct().limit(limit)))
    # One package per logical item; items without a logical identity remain
    # independent packages until a real lifecycle identity is created.
    seen: set[str] = set()
    result: list[OAItem] = []
    for item in rows:
        key = f"logical:{item.logical_item_id}" if item.logical_item_id else f"oa:{item.id}"
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def plan_curation(settings: Settings, engine, *, limit: int | None = None, oa_item_key: str | None = None) -> CurationStats:
    with Session(engine) as session:
        items = _candidate_items(session, oa_item_key=oa_item_key, limit=limit or settings.curation.batch_limit)
        packages = [build_package(session, settings, item) for item in items]
    results = [classify_source(source) for package in packages for source in package.sources]
    return CurationStats(
        packages=len(packages),
        sources=sum(len(package.sources) for package in packages),
        noise_sources=sum(result.disposition == SourceDisposition.NOISE for result in results),
        candidate_sources=sum(result.disposition != SourceDisposition.NOISE for result in results),
        needs_review=sum(package.depth_limit_reached for package in packages),
    )


def _ensure_logical(session: Session, item: OAItem) -> LogicalItem:
    logical = session.get(LogicalItem, item.logical_item_id) if item.logical_item_id else None
    if logical is None:
        logical = LogicalItem(logical_key=f"done:{item.oa_item_key}", title=item.title, lifecycle_status="done")
        session.add(logical)
        session.flush()
        item.logical_item_id = logical.id
    return logical


def _persist_document(session: Session, run: CuratedRun, package: OAPackage, document, ordinal: int) -> tuple[CuratedDecision, str, str | None, str | None]:
    source_map = {source.source_key: source for source in package.sources}
    selected = [source_map[edge.source_key] for edge in document.sources]
    canonical = canonical_key(document, [source.content_sha256 for source in selected])
    knowledge = session.scalar(select(KnowledgeDocument).where(KnowledgeDocument.knowledge_key == canonical))
    previous_path = (
        knowledge.vault_relpath
        if knowledge is not None and knowledge.vault_relpath and knowledge.vault_relpath.startswith("curated/oa/")
        else None
    )
    primary = next((source for edge, source in zip(document.sources, selected) if edge.role == "body"), selected[0])
    source_file = session.get(ArchivedFile, primary.source_file_id) if primary.source_file_id else None
    if source_file is None or source_file.content_object_id is None:
        raise ValueError("curated document has no canonical content object")
    existing_curated_path: str | None = None
    if knowledge is not None and previous_path:
        latest = session.scalar(select(CuratedDecision).where(
            CuratedDecision.knowledge_document_id == knowledge.id,
            CuratedDecision.status == "published",
        ).order_by(CuratedDecision.id.desc()).limit(1))
        if latest is not None:
            prior_edges = session.scalars(select(CuratedDecisionSource).where(
                CuratedDecisionSource.curated_decision_id == latest.id,
            ).order_by(CuratedDecisionSource.ordinal)).all()
            current_signature = [(edge.role, source.content_sha256) for edge, source in zip(document.sources, selected)]
            prior_signature = [(edge.role, edge.content_sha256) for edge in prior_edges]
            if current_signature == prior_signature:
                existing_curated_path = previous_path
    if knowledge is None:
        knowledge = KnowledgeDocument(
            knowledge_key=canonical, content_object_id=source_file.content_object_id,
            active_parse_artifact_id=primary.parse_artifact_id, title=document.normalized_title,
            publish_status="curated",
        )
        session.add(knowledge)
        session.flush()
    decision_hash = hashlib.sha256(document.model_dump_json().encode()).hexdigest()
    row = CuratedDecision(
        curated_run_id=run.id, knowledge_document_id=knowledge.id, ordinal=ordinal,
        status="published" if existing_curated_path else "publishing",
        document_kind=document.document_kind, canonical_key=canonical,
        normalized_title=document.normalized_title, metadata_json=document.model_dump_json(),
        confidence=document.confidence, decision_hash=decision_hash,
        output_relpath=existing_curated_path,
    )
    session.add(row)
    session.flush()
    for source_ordinal, (edge, source) in enumerate(zip(document.sources, selected), 1):
        session.add(CuratedDecisionSource(
            curated_decision_id=row.id, source_file_id=source.source_file_id,
            source_attachment_id=source.source_attachment_id, archive_member_id=source.archive_member_id,
            parse_artifact_id=source.parse_artifact_id, source_key=source.source_key,
            ordinal=source_ordinal, role=edge.role, content_sha256=source.content_sha256,
        ))
        if source.source_file_id:
            archived = session.get(ArchivedFile, source.source_file_id)
            if archived is not None:
                reference = session.scalar(select(SourceReference).where(
                    SourceReference.knowledge_document_id == knowledge.id,
                    SourceReference.source_file_id == archived.id,
                ))
                if reference is None:
                    session.add(SourceReference(
                        knowledge_document_id=knowledge.id, source_file_id=archived.id, oa_item_id=archived.oa_item_id,
                    ))
    relation = session.scalar(select(OAItemDocumentRelation).where(
        OAItemDocumentRelation.logical_item_id == run.logical_item_id,
        OAItemDocumentRelation.knowledge_document_id == knowledge.id,
        OAItemDocumentRelation.source_attachment_id.is_(None),
    ))
    if relation is None:
        session.add(OAItemDocumentRelation(
            logical_item_id=run.logical_item_id, knowledge_document_id=knowledge.id,
            source_attachment_id=None, ordinal=ordinal, role="curated_document",
            is_main_document=True, display_title=document.normalized_title,
        ))
    return row, canonical, existing_curated_path, previous_path


def run_curation(
    settings: Settings,
    engine,
    *,
    limit: int | None = None,
    oa_item_key: str | None = None,
    client=None,
) -> CurationStats:
    if not settings.curation.enabled:
        raise RuntimeError("curation is disabled")
    profile = discover_ollama_profile(
        settings.llm.base_url, settings.llm.model,
        fallback_context_window=settings.llm.context_window_fallback,
        context_window_cap=settings.llm.context_window_cap,
    )
    budget = ContextBudget(
        context_window=min(profile.context_window, 8_192), max_output_tokens=512,
        system_tokens=2000, safety_margin=settings.llm.context_safety_margin,
    )
    max_input = min(settings.curation.max_input_tokens, budget.max_input_tokens)
    model_client = client or make_llm_client(
        settings.llm, max_retries=settings.llm.max_retries, max_tokens=512,
    )
    counters = CurationStats()
    with Session(engine) as session:
        item_ids = [item.id for item in _candidate_items(
            session, oa_item_key=oa_item_key, limit=limit or settings.curation.batch_limit
        )]
    values = counters.model_dump()
    for item_id in item_ids:
        logical_id: int | None = None
        try:
            with Session(engine) as session:
                item = session.get(OAItem, item_id)
                if item is None:
                    continue
                logical = _ensure_logical(session, item)
                logical_id = logical.id
                session.commit()
                package = build_package(session, settings, item)
                package = OAPackage(**{**package.__dict__, "logical_item_id": logical.id})
                signature = package_signature(
                    package, rules_version=RULES_VERSION, prompt_version=PROMPT_VERSION,
                    schema_version=SCHEMA_VERSION, model=settings.llm.model,
                    config_signature=_config_signature(settings),
                )
                existing = session.scalar(select(CuratedRun).where(
                    CuratedRun.logical_item_id == logical.id, CuratedRun.input_signature == signature,
                ))
                values["packages"] += 1
                values["sources"] += len(package.sources)
                if existing and existing.status in {"completed", "needs_review"}:
                    values["skipped"] += 1
                    continue
                if existing:
                    for stale in session.scalars(select(CuratedDecision).where(CuratedDecision.curated_run_id == existing.id)).all():
                        session.delete(stale)
                    run = existing
                    run.status = "running"
                    run.error_code = None
                    run.error_detail = None
                    run.finished_at = None
                else:
                    run = CuratedRun(
                        logical_item_id=logical.id, input_signature=signature, status="running",
                        rules_version=RULES_VERSION, prompt_version=PROMPT_VERSION,
                        schema_version=SCHEMA_VERSION, model_name=settings.llm.model,
                        config_signature=_config_signature(settings),
                    )
                    session.add(run)
                session.commit()
                session.refresh(run)
                coordinator = ResourceCoordinator(engine)
                lease_owner = f"worker-{os.getpid()}:curation:{run.id}"
                lease_id = coordinator.acquire(
                    "local_llm", lease_owner,
                    ttl_seconds=settings.llm.timeout_seconds + 60,
                    uses_local_gpu=settings.llm.uses_local_gpu,
                )
                if lease_id is None:
                    raise RuntimeError("local model resource is busy")
                try:
                    decision = classify_package(
                        package, model_client, max_input_tokens=max_input,
                        confidence_threshold=settings.curation.confidence_threshold,
                    )
                finally:
                    coordinator.release(lease_id, lease_owner)
                if decision.needs_review:
                    run.status = "needs_review"
                    run.error_code = decision.reason_code
                    run.finished_at = datetime.now(timezone.utc)
                    session.commit()
                    values["needs_review"] += 1
                    continue
                if not decision.documents:
                    run.status = "completed"
                    run.finished_at = datetime.now(timezone.utc)
                    session.commit()
                    values["completed"] += 1
                    continue
                publications = []
                for ordinal, document in enumerate(decision.documents, 1):
                    row, canonical, existing_path, previous_path = _persist_document(session, run, package, document, ordinal)
                    publications.append((row.id, canonical, document, existing_path, previous_path))
                session.commit()
                source_map = {source.source_key: source for source in package.sources}
                for row_id, canonical, document, existing_path, previous_path in publications:
                    if existing_path:
                        continue
                    published = publish_document(
                        settings.markdown_root, document, source_map, canonical_id=canonical,
                        decision_version=run.id, fallback_date=package.completed_at or "",
                    )
                    row = session.get(CuratedDecision, row_id)
                    assert row is not None
                    row.output_relpath = published.relpath
                    row.status = "published"
                    knowledge = session.get(KnowledgeDocument, row.knowledge_document_id)
                    if knowledge is not None:
                        knowledge.vault_relpath = published.relpath
                    if previous_path and previous_path != published.relpath:
                        remove_managed_publication(settings.markdown_root, previous_path, canonical_id=canonical)
                run.status = "completed"
                run.finished_at = datetime.now(timezone.utc)
                session.commit()
                values["completed"] += 1
        except Exception as exc:
            values["failed"] += 1
            with Session(engine) as session:
                failed_run = session.scalar(select(CuratedRun).where(
                    CuratedRun.logical_item_id == (logical_id if logical_id is not None else -1),
                    CuratedRun.status == "running",
                ).order_by(CuratedRun.id.desc()).limit(1))
                if failed_run:
                    failed_run.status = "failed"
                    failed_run.error_code = type(exc).__name__
                    failed_run.error_detail = str(exc)[:500]
                    failed_run.finished_at = datetime.now(timezone.utc)
                    session.commit()
    return CurationStats(**values)


def curation_report(engine) -> dict:
    with Session(engine) as session:
        statuses = list(session.scalars(select(CuratedRun.status)))
        decisions = list(session.scalars(select(CuratedDecision)))
    return {
        "runs": len(statuses),
        "statuses": {status: statuses.count(status) for status in sorted(set(statuses))},
        "published_documents": sum(row.status == "published" for row in decisions),
        "report_contains_content": False,
    }


def validate_curation(settings: Settings, engine) -> dict:
    issues: list[dict] = []
    with Session(engine) as session:
        rows = list(session.scalars(select(CuratedDecision).where(CuratedDecision.status == "published")))
        for row in rows:
            if not row.output_relpath:
                issues.append({"decision_id": row.id, "code": "missing_output_path"})
                continue
            for code in validate_publication(settings.markdown_root, row.output_relpath):
                issues.append({"decision_id": row.id, "code": code})
    return {"ok": not issues, "published": len(rows), "issues": issues}
