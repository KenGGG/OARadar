from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from time import monotonic
from typing import Callable
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.constants import LEASE_TTL
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import ArchivedFile, MarkdownExport, OAItem, OAManifestItem, OnlineAuditEvent, OnlineAuditItem, OnlineAuditRun, OperationEvent, OperationJob, PipelineTask
from oa_knowledge.source_roles import AUDIT_ATTACHMENT_ROLES

ATTACHMENT_ROLES = AUDIT_ATTACHMENT_ROLES
AttachmentEvidence = tuple[str, str, int | None, str | None]


@dataclass(frozen=True)
class AuditObservation:
    recognized_attachments: int
    online_inventory_sha256: str | None = None
    online_content_sha256: str | None = None
    depth_limit_reached: bool = False
    attachment_evidence: tuple[AttachmentEvidence, ...] | None = None


def _sorted_evidence(rows: list[AttachmentEvidence] | tuple[AttachmentEvidence, ...]) -> list[AttachmentEvidence]:
    """Deduplicate and sort evidence even when optional values are mixed."""
    return sorted(
        set(rows),
        key=lambda row: (row[0], row[1], -1 if row[2] is None else row[2], row[3] or ""),
    )


def serialize_attachment_evidence(rows: list[AttachmentEvidence] | tuple[AttachmentEvidence, ...]) -> str:
    return json.dumps([
        {"role": role, "key": key, "size": size, "sha256": sha256}
        for role, key, size, sha256 in _sorted_evidence(rows)
    ], ensure_ascii=False, separators=(",", ":"))


def explain_evidence_difference(
    online: list[AttachmentEvidence] | tuple[AttachmentEvidence, ...],
    local: list[AttachmentEvidence] | tuple[AttachmentEvidence, ...],
) -> str:
    """Explain a mismatch without relying on attachment names or file bytes."""
    online_rows, local_rows = _sorted_evidence(online), _sorted_evidence(local)
    if online_rows == local_rows:
        return "exact_match"
    if evidence_is_historical_subset(online_rows, local_rows):
        return "historical_retained"

    def signature(rows, indexes):
        return sorted(tuple("" if row[index] is None else str(row[index]) for index in indexes) for row in rows)

    if signature(online_rows, (0, 1)) == signature(local_rows, (0, 1)):
        return "content_changed"
    if signature(online_rows, (0, 2, 3)) == signature(local_rows, (0, 2, 3)):
        return "attachment_identity_changed"
    if signature(online_rows, (1, 2, 3)) == signature(local_rows, (1, 2, 3)):
        return "attachment_role_changed"
    if signature(online_rows, (2, 3)) == signature(local_rows, (2, 3)):
        return "attachment_metadata_changed"
    return "inventory_changed"


def evidence_is_historical_subset(
    online: list[AttachmentEvidence] | tuple[AttachmentEvidence, ...],
    local: list[AttachmentEvidence] | tuple[AttachmentEvidence, ...],
) -> bool:
    """Return true when every current online byte is retained among extra local history."""
    online_rows, local_rows = set(_sorted_evidence(online)), set(_sorted_evidence(local))
    return online_rows != local_rows and online_rows.issubset(local_rows)


def _deserialize_attachment_evidence(raw: str | None) -> list[AttachmentEvidence]:
    if not raw:
        return []
    return [
        (row["role"], row["key"], row.get("size"), row.get("sha256"))
        for row in json.loads(raw)
    ]


def fingerprint_attachments(
    rows: list[AttachmentEvidence],
) -> tuple[str, str | None]:
    """Return order-independent identity and byte-content fingerprints."""
    normalized = _sorted_evidence(rows)
    inventory = [[role, key] for role, key, _size, _sha256 in normalized]
    inventory_hash = hashlib.sha256(
        json.dumps(inventory, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    if any(not sha256 for _role, _key, _size, sha256 in normalized):
        return inventory_hash, None
    content = [[role, key, size, sha256] for role, key, size, sha256 in normalized]
    return inventory_hash, hashlib.sha256(
        json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def classify_evidence(
    *, recognized: int, downloaded: int,
    online_inventory: str | None, local_inventory: str | None,
    online_content: str | None, local_content: str | None,
    depth_limit_reached: bool,
) -> str:
    if depth_limit_reached:
        return "depth_limit_reached"
    if recognized > downloaded:
        return "missing_download"
    if online_inventory and local_inventory and online_inventory != local_inventory:
        return "inventory_mismatch"
    if recognized < downloaded:
        return "historical_retained"
    if online_content is None:
        return "content_unverified"
    if local_content is None or online_content != local_content:
        return "content_mismatch"
    return "matched"


def _repair_item_from_persisted_evidence(item: OnlineAuditItem) -> bool:
    """Recompute stale summary columns from the durable per-attachment ledger."""
    if item.comparison_reason is None:
        return False
    try:
        online_rows = _deserialize_attachment_evidence(item.online_evidence_json)
        local_rows = _deserialize_attachment_evidence(item.local_evidence_json)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    online_inventory, online_content = fingerprint_attachments(online_rows)
    local_inventory, local_content = fingerprint_attachments(local_rows)
    reason = explain_evidence_difference(online_rows, local_rows)
    status = classify_evidence(
        recognized=(
            item.recognized_attachments
            if item.recognized_attachments is not None
            else len(online_rows)
        ),
        downloaded=item.downloaded_attachments,
        online_inventory=online_inventory,
        local_inventory=local_inventory,
        online_content=online_content,
        local_content=local_content,
        depth_limit_reached=item.depth_limit_reached,
    )
    if evidence_is_historical_subset(online_rows, local_rows):
        status = "historical_retained"
    before = (
        item.status, item.comparison_reason,
        item.online_inventory_sha256, item.local_inventory_sha256,
        item.online_content_sha256, item.local_content_sha256,
    )
    after = (status, reason, online_inventory, local_inventory, online_content, local_content)
    if before == after:
        return False
    (
        item.status, item.comparison_reason,
        item.online_inventory_sha256, item.local_inventory_sha256,
        item.online_content_sha256, item.local_content_sha256,
    ) = after
    return True


def unique_capture_attachment_count(capture) -> int:
    attachments = [row for row in capture.attachments if row.file_role in ATTACHMENT_ROLES]
    attachments += [row for container in capture.related_containers for row in container.attachments if row.file_role in ATTACHMENT_ROLES]
    return len({row.attachment_key for row in attachments})

def canonical_downloaded_count(*, recognized: int, verified_rows: int, unique_hashes: int) -> int:
    if verified_rows < recognized:
        return verified_rows
    return max(recognized, unique_hashes)


def classify_attachment_counts(recognized: int, database: int, downloaded: int, markdown: int) -> str:
    """Classify attachment inventory independently from downstream Markdown lag."""
    if recognized > downloaded:
        return "missing_download"
    if recognized < downloaded:
        return "historical_retained"
    return "matched"


def _event(session: Session, run_id: int, event_type: str, message: str, *, item_id: int | None = None, level: str = "info", details: dict | None = None) -> None:
    sequence = (session.scalar(select(func.max(OnlineAuditEvent.sequence)).where(OnlineAuditEvent.run_id == run_id)) or 0) + 1
    session.add(OnlineAuditEvent(run_id=run_id, item_id=item_id, sequence=sequence, event_type=event_type, level=level,
                                 message=message, details_json=json.dumps(details or {}, ensure_ascii=False)))


def _sanitize_error(exc: Exception) -> str:
    text = str(exc).replace("\n", " ")[:1000]
    text = re.sub(r"(?i)(authorization|cookie|token|secret)\s*[:=]?\s*[^;\s]+(?:\s+[^;\s]+)?", r"\1=[redacted]", text)
    return text


def start_audit(settings: Settings) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            active = session.scalar(select(OnlineAuditRun).where(OnlineAuditRun.status.in_(("queued", "running", "pause_requested", "paused"))).order_by(OnlineAuditRun.id.desc()).limit(1))
            if active:
                active_job = session.get(OperationJob, active.job_id) if active.job_id else None
                if active_job is not None and active_job.status in {"queued", "running", "paused"}:
                    return {"run_id": active.id, "status": active.status, "total_items": active.total_items, "created": False}
                active.status = "failed"
                active.finished_at = datetime.now(timezone.utc)
                active.current_oa_item_key = None
                _event(
                    session, active.id, "orphaned_job_detected",
                    "审计作业已终止，主记录同步标记失败", level="error",
                    details={"job_status": active_job.status if active_job else "missing"},
                )
                session.flush()
            manifests = session.scalars(select(OAManifestItem).order_by(OAManifestItem.id)).all()
            job = OperationJob(job_key=f"online-audit-{uuid4().hex[:12]}", job_type="online_audit", status="queued",
                               idempotency_key=f"online-audit-{uuid4().hex}", parameters_json="{}", progress_total=len(manifests))
            job.events.append(OperationEvent(sequence=1, event_type="created", status="queued", details_json=json.dumps({"target_count": len(manifests)})))
            session.add(job); session.flush()
            run = OnlineAuditRun(job_id=job.id, status="queued", total_items=len(manifests))
            session.add(run); session.flush()
            session.add_all([OnlineAuditItem(run_id=run.id, manifest_item_id=row.id, oa_item_key=row.oa_item_key,
                workitem_id_text=row.workitem_id_text, title=row.title) for row in manifests])
            _event(session, run.id, "audit_created", f"在线审计已创建，共 {len(manifests)} 个已办事项")
            session.commit()
            return {"run_id": run.id, "status": run.status, "total_items": run.total_items, "created": True}
    finally:
        engine.dispose()


def pause_audit(settings: Settings, run_id: int) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            run = session.get(OnlineAuditRun, run_id)
            if not run or run.status not in {"queued", "running"}: raise ValueError("audit is not running")
            run.status = "pause_requested"; run.pause_requested_at = datetime.now(timezone.utc)
            job = session.get(OperationJob, run.job_id) if run.job_id else None
            if job and job.status == "queued": job.status = "paused"; run.status = "paused"
            _event(session, run.id, "pause_requested", "已请求安全暂停，将在当前事项完成后停止")
            session.commit(); return {"run_id": run.id, "status": run.status}
    finally: engine.dispose()


def resume_audit(settings: Settings, run_id: int) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            run = session.get(OnlineAuditRun, run_id)
            if not run or run.status not in {"paused", "pause_requested"}: raise ValueError("audit is not paused")
            run.status = "queued"; run.pause_requested_at = None
            job = session.get(OperationJob, run.job_id) if run.job_id else None
            if job: job.status = "queued"; job.finished_at = None; job.lease_owner = None; job.lease_expires_at = None
            _event(session, run.id, "audit_resumed", "在线审计已继续")
            session.commit(); return {"run_id": run.id, "status": run.status}
    finally: engine.dispose()


def restart_audit(settings: Settings) -> dict:
    """Preserve the previous ledger, supersede it, and create a fresh zero-progress run."""
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            active = session.scalar(select(OnlineAuditRun).where(OnlineAuditRun.status.in_(("queued", "running", "pause_requested", "paused"))).order_by(OnlineAuditRun.id.desc()).limit(1))
            if active:
                active.status = "superseded"; active.current_oa_item_key = None; active.finished_at = datetime.now(timezone.utc)
                job = session.get(OperationJob, active.job_id) if active.job_id else None
                if job:
                    job.status = "cancelled"; job.finished_at = active.finished_at; job.lease_owner = None; job.lease_expires_at = None
                _event(session, active.id, "audit_superseded", "旧审计已保留并由全新审计替代")
                session.commit()
    finally:
        engine.dispose()
    return start_audit(settings)


def fail_audit(settings: Settings, run_id: int, error_code: str) -> None:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            run = session.get(OnlineAuditRun, run_id)
            if not run: return
            run.status = "failed"; run.current_oa_item_key = None; run.finished_at = datetime.now(timezone.utc)
            _event(session, run.id, "audit_failed", "在线审计运行失败", level="error", details={"error_code": error_code})
            session.commit()
    finally: engine.dispose()


def _local_counts(
    session: Session, item: OnlineAuditItem,
) -> tuple[int, int, int, str | None, str | None, list[AttachmentEvidence]]:
    oa = session.scalar(select(OAItem).where(OAItem.oa_item_key == item.oa_item_key))
    if not oa: return 0, 0, 0, None, None, []
    ids = list(session.scalars(select(ArchivedFile.id).where(ArchivedFile.oa_item_id == oa.id, ArchivedFile.file_role.in_(ATTACHMENT_ROLES))))
    total = len(ids)
    verified = session.scalars(select(ArchivedFile).where(
        ArchivedFile.oa_item_id == oa.id,
        ArchivedFile.file_role.in_(ATTACHMENT_ROLES),
        ArchivedFile.download_status == "verified",
    )).all()
    unique_hashes = len({row.sha256 for row in verified if row.sha256})
    downloaded = canonical_downloaded_count(recognized=item.recognized_attachments or 0, verified_rows=len(verified), unique_hashes=unique_hashes)
    markdown = session.scalar(select(func.count()).select_from(MarkdownExport).where(MarkdownExport.source_file_id.in_(ids), MarkdownExport.status == "success")) if ids else 0
    evidence = [
        (row.file_role, row.attachment_key, row.size_bytes, row.sha256)
        for row in verified
    ]
    inventory_hash, content_hash = fingerprint_attachments(evidence)
    return total, downloaded, markdown or 0, inventory_hash, content_hash, evidence


FINAL_ITEM_STATUSES = frozenset({
    "matched", "missing_download", "historical_retained", "inventory_mismatch",
    "content_mismatch", "content_unverified", "depth_limit_reached", "access_failed",
})
MISMATCH_ITEM_STATUSES = FINAL_ITEM_STATUSES - {"matched", "access_failed"}


def _refresh_run_counts(session: Session, run: OnlineAuditRun) -> None:
    groups = dict(session.execute(
        select(OnlineAuditItem.status, func.count())
        .where(OnlineAuditItem.run_id == run.id)
        .group_by(OnlineAuditItem.status)
    ).all())
    run.completed_items = sum(groups.get(status, 0) for status in FINAL_ITEM_STATUSES)
    run.matched_items = groups.get("matched", 0)
    run.mismatch_items = sum(groups.get(status, 0) for status in MISMATCH_ITEM_STATUSES)
    run.access_failed_items = groups.get("access_failed", 0)


def enroll_new_manifest_items(session: Session, run: OnlineAuditRun) -> int:
    """Attach manifest rows discovered after an audit run was created.

    A long full audit spans many scheduled Done-list refreshes.  Its denominator
    must therefore follow the durable manifest instead of remaining a stale
    point-in-time count.  A completed run is reopened before migration if a
    later refresh reveals an unaudited item.
    """
    already_enrolled = select(OnlineAuditItem.id).where(
        OnlineAuditItem.run_id == run.id,
        OnlineAuditItem.oa_item_key == OAManifestItem.oa_item_key,
    ).exists()
    manifests = session.scalars(
        select(OAManifestItem)
        .where(~already_enrolled)
        .order_by(OAManifestItem.id)
    ).all()
    if not manifests:
        return 0
    session.add_all([
        OnlineAuditItem(
            run_id=run.id,
            manifest_item_id=row.id,
            oa_item_key=row.oa_item_key,
            workitem_id_text=row.workitem_id_text,
            title=row.title,
        )
        for row in manifests
    ])
    session.flush()
    run.total_items = int(session.scalar(
        select(func.count()).select_from(OnlineAuditItem).where(
            OnlineAuditItem.run_id == run.id,
        )
    ) or 0)
    job = session.get(OperationJob, run.job_id) if run.job_id else None
    if job is not None:
        job.progress_total = run.total_items
    if run.status == "completed":
        run.status = "queued"
        run.finished_at = None
        if job is not None:
            job.status = "queued"
            job.finished_at = None
            job.lease_owner = None
            job.lease_expires_at = None
            job.heartbeat_at = None
            job.last_error_code = None
    _event(
        session,
        run.id,
        "manifest_items_enrolled",
        f"已将 {len(manifests)} 个新增已办事项纳入在线审计",
        details={"added_count": len(manifests), "total_items": run.total_items},
    )
    return len(manifests)


def _enqueue_missing_download(session: Session, run: OnlineAuditRun, item: OnlineAuditItem) -> bool:
    """Queue a read-only recapture without overwriting mismatch evidence."""
    if not item.workitem_id_text:
        return False
    digest = hashlib.sha256(f"{run.id}:{item.oa_item_key}".encode()).hexdigest()[:24]
    key = f"online-audit:{run.id}:{digest}:supplement-v1"
    if session.scalar(select(PipelineTask.id).where(PipelineTask.idempotency_key == key)):
        return False
    session.add(PipelineTask(
        queue_name="realtime_done", priority=10,
        logical_item_key=item.oa_item_key,
        stage="done_capture_and_archive",
        idempotency_key=key,
        payload_json=json.dumps({
            "manifest_id": item.manifest_item_id,
            "online_audit_run_id": run.id,
            "reason": "missing_download",
        }, ensure_ascii=False),
    ))
    return True


def requeue_supplemented_item(
    session: Session,
    run_id: int,
    oa_item_key: str,
) -> bool:
    """Reopen a missing-download item after its read-only supplement succeeded.

    The capture task and audit run are separate durable queues.  A successful
    capture therefore has to put the item back into the audit queue so its new
    local bytes are compared with fresh online evidence before migration can
    trust the result.
    """
    run = session.get(OnlineAuditRun, run_id)
    if run is None:
        return False
    item = session.scalar(select(OnlineAuditItem).where(
        OnlineAuditItem.run_id == run_id,
        OnlineAuditItem.oa_item_key == oa_item_key,
    ))
    if item is None or item.status != "missing_download":
        return False

    item.status = "pending"
    item.started_at = None
    item.finished_at = None
    item.elapsed_seconds = None
    item.error_code = None
    item.error_detail = None
    item.comparison_reason = None
    _refresh_run_counts(session, run)

    run.current_oa_item_key = None
    run.finished_at = None
    if run.status not in {"paused", "pause_requested"}:
        run.status = "queued"
        job = session.get(OperationJob, run.job_id) if run.job_id else None
        if job is not None:
            job.status = "queued"
            job.finished_at = None
            job.lease_owner = None
            job.lease_expires_at = None
            job.heartbeat_at = None
            job.last_error_code = None
    _event(
        session,
        run.id,
        "supplement_completed_recheck_queued",
        "补下载已完成，事项已重新排队核验",
        item_id=item.id,
    )
    return True


def requeue_changed_item_for_latest_audit(
    session: Session,
    oa_item_key: str,
) -> bool:
    """Invalidate terminal audit evidence after a scheduled Done recapture."""
    run = session.scalar(
        select(OnlineAuditRun)
        .where(OnlineAuditRun.status.in_((
            "queued", "running", "pause_requested", "paused", "completed",
        )))
        .order_by(OnlineAuditRun.id.desc())
        .limit(1)
    )
    if run is None:
        return False
    enrolled = enroll_new_manifest_items(session, run)
    item = session.scalar(select(OnlineAuditItem).where(
        OnlineAuditItem.run_id == run.id,
        OnlineAuditItem.oa_item_key == oa_item_key,
    ))
    if item is None or item.status in {"pending", "running"}:
        return bool(enrolled)

    item.status = "pending"
    item.started_at = None
    item.finished_at = None
    item.elapsed_seconds = None
    item.error_code = None
    item.error_detail = None
    item.comparison_reason = None
    item.online_inventory_sha256 = None
    item.online_content_sha256 = None
    item.online_evidence_json = "[]"
    _refresh_run_counts(session, run)
    run.current_oa_item_key = None
    run.finished_at = None
    if run.status not in {"paused", "pause_requested"}:
        run.status = "queued"
        job = session.get(OperationJob, run.job_id) if run.job_id else None
        if job is not None:
            job.status = "queued"
            job.finished_at = None
            job.lease_owner = None
            job.lease_expires_at = None
            job.heartbeat_at = None
            job.last_error_code = None
    _event(
        session,
        run.id,
        "done_recapture_recheck_queued",
        "已办事项重新归档后已使旧核验证据失效",
        item_id=item.id,
    )
    return True


def execute_audit(
    settings: Settings,
    run_id: int,
    *,
    inspect_item: Callable[[OnlineAuditItem], AuditObservation],
    max_items: int | None = None,
    max_seconds: float | None = None,
) -> None:
    if max_items is not None and max_items < 1:
        raise ValueError("max_items must be positive")
    if max_seconds is not None and max_seconds <= 0:
        raise ValueError("max_seconds must be positive")
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            run = session.get(OnlineAuditRun, run_id)
            if not run: raise LookupError("audit run not found")
            enroll_new_manifest_items(session, run)
            interrupted = session.scalars(select(OnlineAuditItem).where(OnlineAuditItem.run_id == run_id, OnlineAuditItem.status == "running")).all()
            for item in interrupted:
                item.status = "pending"; item.started_at = None
            if interrupted:
                _event(session, run.id, "items_recovered", f"已恢复 {len(interrupted)} 个中断事项")
            pre_evidence = session.scalars(select(OnlineAuditItem).where(
                OnlineAuditItem.run_id == run_id,
                OnlineAuditItem.status.in_(FINAL_ITEM_STATUSES - {"access_failed"}),
                OnlineAuditItem.comparison_reason.is_(None),
            )).all()
            for item in pre_evidence:
                item.status = "pending"
                item.started_at = None
                item.finished_at = None
                item.elapsed_seconds = None
            if pre_evidence:
                _event(
                    session, run.id, "pre_evidence_items_requeued",
                    f"已将 {len(pre_evidence)} 个旧证据事项重新排队核验",
                )
                _refresh_run_counts(session, run)
            repaired_evidence = 0
            candidates = session.scalars(select(OnlineAuditItem).where(
                OnlineAuditItem.run_id == run_id,
                OnlineAuditItem.status.in_(FINAL_ITEM_STATUSES - {"access_failed"}),
                OnlineAuditItem.online_evidence_json.is_not(None),
                OnlineAuditItem.local_evidence_json.is_not(None),
                OnlineAuditItem.comparison_reason.is_not(None),
            )).all()
            for item in candidates:
                repaired_evidence += int(_repair_item_from_persisted_evidence(item))
            if repaired_evidence:
                _event(
                    session, run.id, "persisted_evidence_repaired",
                    f"已从逐附件证据修复 {repaired_evidence} 个汇总状态",
                )
                _refresh_run_counts(session, run)
            now = datetime.now(timezone.utc)
            run.status = "running"; run.started_at = run.started_at or now
            job = session.get(OperationJob, run.job_id) if run.job_id else None
            if job:
                job.status = "running"
                job.started_at = job.started_at or now
                job.heartbeat_at = now
                job.lease_expires_at = now + LEASE_TTL
                job.last_error_code = None
            _event(session, run.id, "audit_started", "开始在线遍历已办事项"); session.commit()
        processed_in_batch = 0
        batch_started = monotonic()
        while True:
            with Session(engine) as session:
                run = session.get(OnlineAuditRun, run_id); assert run
                if run.pause_requested_at:
                    run.status = "paused"; run.current_oa_item_key = None
                    job = session.get(OperationJob, run.job_id) if run.job_id else None
                    if job: job.status = "paused"; job.lease_owner = None; job.lease_expires_at = None
                    _event(session, run.id, "audit_paused", "在线审计已安全暂停"); session.commit(); return
                item = session.scalar(select(OnlineAuditItem).where(OnlineAuditItem.run_id == run_id, OnlineAuditItem.status == "pending").order_by(OnlineAuditItem.id).limit(1))
                if not item: break
                item.status = "running"; item.started_at = datetime.now(timezone.utc); run.current_oa_item_key = item.oa_item_key
                item.error_code = None; item.error_detail = None
                _event(session, run.id, "item_started", f"正在复查：{item.title}", item_id=item.id); session.commit()
                started = monotonic()
                try:
                    observation = inspect_item(item)
                    item = session.get(OnlineAuditItem, item.id); assert item
                    item.recognized_attachments = observation.recognized_attachments
                    (
                        item.database_attachments, item.downloaded_attachments,
                        item.markdown_attachments, item.local_inventory_sha256,
                        item.local_content_sha256, local_evidence,
                    ) = _local_counts(session, item)
                    item.online_inventory_sha256 = observation.online_inventory_sha256
                    item.online_content_sha256 = observation.online_content_sha256
                    item.depth_limit_reached = observation.depth_limit_reached
                    online_evidence = observation.attachment_evidence
                    item.online_evidence_json = serialize_attachment_evidence(online_evidence or ())
                    item.local_evidence_json = serialize_attachment_evidence(local_evidence)
                    item.comparison_reason = (
                        explain_evidence_difference(online_evidence, local_evidence)
                        if online_evidence is not None else "evidence_unavailable"
                    )
                    if observation.online_inventory_sha256 is None:
                        item.status = classify_attachment_counts(
                            item.recognized_attachments, item.database_attachments,
                            item.downloaded_attachments, item.markdown_attachments,
                        )
                    else:
                        item.status = classify_evidence(
                            recognized=item.recognized_attachments,
                            downloaded=item.downloaded_attachments,
                            online_inventory=item.online_inventory_sha256,
                            local_inventory=item.local_inventory_sha256,
                            online_content=item.online_content_sha256,
                            local_content=item.local_content_sha256,
                            depth_limit_reached=item.depth_limit_reached,
                        )
                        if (
                            not item.depth_limit_reached
                            and online_evidence is not None
                            and evidence_is_historical_subset(online_evidence, local_evidence)
                        ):
                            item.status = "historical_retained"
                    if item.status == "missing_download" and _enqueue_missing_download(session, run, item):
                        _event(
                            session, run.id, "supplement_enqueued",
                            "已将缺失附件事项加入安全补下载队列",
                            item_id=item.id, details={"status": item.status},
                        )
                    event_type, level = "item_completed", "info" if item.status == "matched" else "warning"
                    _event(session, run.id, event_type, f"复查完成：{item.title}", item_id=item.id,
                           level=level, details={"recognized": item.recognized_attachments, "downloaded": item.downloaded_attachments, "status": item.status})
                except Exception as exc:
                    item = session.get(OnlineAuditItem, item.id); assert item
                    item.status = "access_failed"; item.error_code = "OA_ACCESS_ERROR"; item.error_detail = _sanitize_error(exc)
                    _event(session, run.id, "item_failed", f"OA访问失败：{item.title}", item_id=item.id, level="error", details={"error_code": item.error_code})
                item.finished_at = datetime.now(timezone.utc); item.elapsed_seconds = round(monotonic() - started, 3)
                run = session.get(OnlineAuditRun, run_id); assert run
                _refresh_run_counts(session, run)
                job = session.get(OperationJob, run.job_id) if run.job_id else None
                if job:
                    now = datetime.now(timezone.utc)
                    job.progress_current = run.completed_items
                    job.heartbeat_at = now
                    job.lease_expires_at = now + LEASE_TTL
                session.commit()
                processed_in_batch += 1
                if max_items is not None and processed_in_batch >= max_items:
                    break
                if max_seconds is not None and monotonic() - batch_started >= max_seconds:
                    break
        with Session(engine) as session:
            run = session.get(OnlineAuditRun, run_id); assert run
            enroll_new_manifest_items(session, run)
            has_pending = session.scalar(select(OnlineAuditItem.id).where(
                OnlineAuditItem.run_id == run_id,
                OnlineAuditItem.status == "pending",
            ).limit(1)) is not None
            if has_pending:
                now = datetime.now(timezone.utc)
                run.status = "queued"
                run.current_oa_item_key = None
                job = session.get(OperationJob, run.job_id) if run.job_id else None
                if job:
                    job.status = "queued"
                    job.heartbeat_at = now
                    job.lease_owner = None
                    job.lease_expires_at = None
                _event(
                    session, run.id, "audit_batch_yielded",
                    f"本批已核验 {processed_in_batch} 项，已让出执行权",
                )
                session.commit()
                return
        with Session(engine) as session:
            run = session.get(OnlineAuditRun, run_id); assert run
            job = session.get(OperationJob, run.job_id) if run.job_id else None
            retry_parameters = json.loads(job.parameters_json or "{}") if job else {}
            failed_items = session.scalars(select(OnlineAuditItem).where(
                OnlineAuditItem.run_id == run_id,
                OnlineAuditItem.status == "access_failed",
            )).all()
            if failed_items and int(retry_parameters.get("access_retry_round", 0)) < 1:
                for item in failed_items:
                    item.status = "pending"
                    item.started_at = None
                    item.finished_at = None
                    item.elapsed_seconds = None
                retry_parameters["access_retry_round"] = 1
                _refresh_run_counts(session, run)
                run.status = "queued"
                run.current_oa_item_key = None
                if job:
                    job.status = "queued"
                    job.parameters_json = json.dumps(retry_parameters, sort_keys=True)
                    job.progress_current = run.completed_items
                    job.heartbeat_at = datetime.now(timezone.utc)
                    job.lease_owner = None
                    job.lease_expires_at = None
                _event(
                    session, run.id, "access_failures_requeued",
                    f"已将 {len(failed_items)} 个访问失败事项重新排队一次",
                    details={"retry_round": 1, "item_count": len(failed_items)},
                )
                session.commit()
                return
            _refresh_run_counts(session, run)
            run.status = "completed"; run.current_oa_item_key = None; run.finished_at = datetime.now(timezone.utc)
            if job:
                job.status = "completed"; job.progress_current = run.completed_items
                job.finished_at = run.finished_at; job.last_error_code = None
                job.lease_owner = None; job.lease_expires_at = None
            _event(session, run.id, "audit_completed", "在线审计已完成"); session.commit()
    finally: engine.dispose()


def audit_view(settings: Settings, run_id: int | None = None, *, item_page: int = 1, item_page_size: int = 50) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            run = session.get(OnlineAuditRun, run_id) if run_id else session.scalar(select(OnlineAuditRun).order_by(OnlineAuditRun.id.desc()).limit(1))
            if not run:
                from oa_knowledge.markdown_queue import queue_view
                from oa_knowledge.web.status import archive_date_status
                return {"run": None, "items": [], "events": [], "errors": [], "comparison_reasons": {}, "markdown_queue": queue_view(settings), "archive_dates": archive_date_status(settings), "item_pagination": {"page": item_page, "page_size": item_page_size, "total": 0, "pages": 0}}
            item_total = session.scalar(select(func.count()).select_from(OnlineAuditItem).where(OnlineAuditItem.run_id == run.id)) or 0
            page_count = (item_total + item_page_size - 1) // item_page_size
            items = session.scalars(select(OnlineAuditItem).where(OnlineAuditItem.run_id == run.id).order_by(OnlineAuditItem.id).offset((item_page - 1) * item_page_size).limit(item_page_size)).all()
            events = session.scalars(select(OnlineAuditEvent).where(OnlineAuditEvent.run_id == run.id).order_by(OnlineAuditEvent.sequence.desc()).limit(200)).all()
            error_rows = session.execute(select(OnlineAuditItem.error_code, func.count()).where(OnlineAuditItem.run_id == run.id, OnlineAuditItem.error_code.is_not(None)).group_by(OnlineAuditItem.error_code)).all()
            reason_rows = session.execute(
                select(OnlineAuditItem.comparison_reason, func.count())
                .where(OnlineAuditItem.run_id == run.id, OnlineAuditItem.comparison_reason.is_not(None))
                .group_by(OnlineAuditItem.comparison_reason)
            ).all()
            missing_download_items = session.scalar(select(func.count()).select_from(OnlineAuditItem).where(OnlineAuditItem.run_id == run.id, OnlineAuditItem.status == "missing_download")) or 0
            local_extra_items = session.scalar(select(func.count()).select_from(OnlineAuditItem).where(OnlineAuditItem.run_id == run.id, OnlineAuditItem.status == "historical_retained")) or 0
            markdown_pending_items = session.scalar(select(func.count()).select_from(OnlineAuditItem).where(OnlineAuditItem.run_id == run.id, OnlineAuditItem.downloaded_attachments > OnlineAuditItem.markdown_attachments, OnlineAuditItem.status != "pending")) or 0
            from oa_knowledge.markdown_queue import queue_view
            from oa_knowledge.web.status import archive_date_status
            return {"run": {**{key: getattr(run, key) for key in ("id", "status", "total_items", "completed_items", "matched_items", "mismatch_items", "access_failed_items", "current_oa_item_key")},
                    "missing_download_items": missing_download_items, "local_extra_items": local_extra_items, "markdown_pending_items": markdown_pending_items,
                    "started_at": run.started_at.isoformat() if run.started_at else None, "finished_at": run.finished_at.isoformat() if run.finished_at else None},
                "items": [{key: getattr(row, key) for key in ("id", "oa_item_key", "title", "status", "recognized_attachments", "database_attachments", "downloaded_attachments", "markdown_attachments", "online_inventory_sha256", "local_inventory_sha256", "online_content_sha256", "local_content_sha256", "comparison_reason", "depth_limit_reached", "error_code", "error_detail", "elapsed_seconds")} for row in items],
                "events": [{"sequence": row.sequence, "event_type": row.event_type, "level": row.level, "message": row.message, "details": json.loads(row.details_json), "created_at": row.created_at.isoformat() if row.created_at else None} for row in events],
                "errors": [{"error_code": code, "count": count} for code, count in error_rows],
                "comparison_reasons": {reason: count for reason, count in reason_rows},
                "markdown_queue": queue_view(settings), "archive_dates": archive_date_status(settings), "item_pagination": {"page": item_page, "page_size": item_page_size, "total": item_total, "pages": page_count}}
    finally: engine.dispose()
