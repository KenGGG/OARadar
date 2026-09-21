"""Bring every eligible Done manifest row back onto the durable pipeline."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.db.models import ArchivedFile, OAItem, OAManifestItem, ParseJob, PipelineEvent, PipelineTask
from oa_knowledge.production_pipeline import QUEUE_PRIORITY
from oa_knowledge.source_roles import MARKDOWN_SOURCE_ROLES
from oa_knowledge.web.delivery_facts import delivery_facts_map


_DOWNLOAD_STAGES = frozenset({"done_capture_and_archive", "archive_verify"})
_MARKDOWN_STAGES = frozenset({"attachment_inventory", "parse", "source_publish", "classify", "index_publish"})


@dataclass(frozen=True)
class DoneConvergenceReport:
    eligible: int
    excluded: int
    download_created: int
    download_requeued: int
    markdown_created: int
    markdown_requeued: int
    attention: int


class DoneConvergencePlanner:
    """Plan or apply idempotent repairs using local database evidence only."""

    def __init__(self, engine, settings) -> None:
        self.engine = engine
        self.settings = settings

    def plan(self, *, apply: bool) -> DoneConvergenceReport:
        counts = {
            "eligible": 0, "excluded": 0, "download_created": 0,
            "download_requeued": 0, "markdown_created": 0,
            "markdown_requeued": 0, "attention": 0,
        }
        with Session(self.engine) as session:
            manifests = list(session.scalars(select(OAManifestItem).order_by(OAManifestItem.id)))
            keys = [row.oa_item_key for row in manifests]
            items = {
                row.oa_item_key: row for row in session.scalars(
                    select(OAItem).where(OAItem.source_channel == "done", OAItem.oa_item_key.in_(keys))
                )
            } if keys else {}
            delivery = delivery_facts_map(session, list(items.values()))
            complete_items = {
                item.oa_item_key for item in items.values()
                if delivery[item.id]["status"] == "complete"
            }
            tasks_by_key: dict[str, list[PipelineTask]] = {}
            if keys:
                for task in session.scalars(
                    select(PipelineTask).where(PipelineTask.logical_item_key.in_(keys))
                    .order_by(PipelineTask.logical_item_key, PipelineTask.created_at.desc(), PipelineTask.id.desc())
                ):
                    tasks_by_key.setdefault(task.logical_item_key, []).append(task)

            for manifest in manifests:
                if manifest.processing_status == "skipped" or bool((manifest.matched_exclusion_keyword or "").strip()):
                    counts["excluded"] += 1
                    continue
                counts["eligible"] += 1
                item = items.get(manifest.oa_item_key)
                archive_ready = bool(
                    manifest.processing_status == "no_attachment" and manifest.no_attachment_confirmed
                    or manifest.processing_status == "downloaded" and item and item.archive_relpath
                )
                phase = "markdown" if archive_ready else "download"
                if phase == "markdown" and manifest.oa_item_key in complete_items:
                    continue
                stages = _MARKDOWN_STAGES if phase == "markdown" else _DOWNLOAD_STAGES
                relevant = next((task for task in tasks_by_key.get(manifest.oa_item_key, ()) if task.stage in stages), None)
                if relevant and relevant.status in {"queued", "running"}:
                    continue
                if relevant and relevant.status == "failed":
                    if relevant.recoverable:
                        counts[f"{phase}_requeued"] += 1
                        if apply:
                            self._requeue(session, relevant, item=item if phase == "markdown" else None)
                    else:
                        counts["attention"] += 1
                    continue

                stage = "attachment_inventory" if phase == "markdown" else "done_capture_and_archive"
                key = self._idempotency_key(manifest, item, phase)
                existing = session.scalar(select(PipelineTask).where(PipelineTask.idempotency_key == key))
                if existing is not None:
                    if existing.status == "failed" and existing.recoverable:
                        counts[f"{phase}_requeued"] += 1
                        if apply:
                            self._requeue(session, existing, item=item if phase == "markdown" else None)
                    elif existing.status == "failed":
                        counts["attention"] += 1
                    continue
                counts[f"{phase}_created"] += 1
                if apply:
                    session.add(PipelineTask(
                        queue_name="markdown_delivery" if phase == "markdown" else "realtime_done",
                        priority=QUEUE_PRIORITY["markdown_delivery" if phase == "markdown" else "realtime_done"],
                        logical_item_key=manifest.oa_item_key,
                        stage=stage,
                        idempotency_key=key,
                        payload_json=json.dumps({"reason": "done_convergence"}),
                    ))
            if apply:
                session.commit()
            else:
                session.rollback()
        return DoneConvergenceReport(**counts)

    @staticmethod
    def _idempotency_key(manifest: OAManifestItem, item: OAItem | None, phase: str) -> str:
        if phase == "download":
            return f"converge-download:{manifest.oa_item_key}:v1"
        signature_source = (item.archive_relpath if item else None) or manifest.archive_relpath or "no-attachment"
        signature = hashlib.sha256(signature_source.encode("utf-8")).hexdigest()[:16]
        return f"converge-markdown:{manifest.oa_item_key}:{signature}:v1"

    @staticmethod
    def _requeue(session: Session, task: PipelineTask, *, item: OAItem | None = None) -> None:
        task.status = "queued"
        task.attempts = 0
        task.progress_current = 0
        task.progress_total = None
        task.error_code = None
        task.last_error = None
        task.next_retry_at = None
        task.started_at = None
        task.finished_at = None
        task.lease_owner = None
        task.lease_expires_at = None
        if item is not None:
            source_ids = select(ArchivedFile.id).where(
                ArchivedFile.oa_item_id == item.id,
                ArchivedFile.file_role.in_(MARKDOWN_SOURCE_ROLES),
            )
            for job in session.scalars(select(ParseJob).where(
                ParseJob.file_id.in_(source_ids), ParseJob.status == "failed",
            )):
                job.status = "queued"
                job.attempts = 0
                job.error_code = None
        session.add(PipelineEvent(
            task_id=task.id, event_type="convergence_requeued", stage=task.stage,
            status="queued", details_json=json.dumps({"reason": "done_convergence"}),
        ))
