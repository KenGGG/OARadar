"""Bring every eligible Done manifest row back onto the durable pipeline."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from oa_knowledge.db.models import ArchivedFile, ClassificationDecision, OAItem, OAManifestItem, ParseJob, PipelineEvent, PipelineTask
from oa_knowledge.production_pipeline import QUEUE_PRIORITY
from oa_knowledge.storage_paths import resolve_data_path
from oa_knowledge.source_roles import MARKDOWN_SOURCE_ROLES
from oa_knowledge.web.delivery_facts import delivery_facts_map


_DOWNLOAD_STAGES = frozenset({"done_capture_and_archive", "archive_verify"})
_MARKDOWN_STAGES = frozenset({"attachment_inventory", "parse", "source_publish", "classify", "index_publish"})
_LEGACY_RETRYABLE_ERRORS = frozenset({
    "DAILY_DELIVERY_PARTIAL", "DAILY_DELIVERY_FAILED", "DAILY_DELIVERY_ERROR",
})


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
        actions: list[tuple[str, str, str, int | None, int | None, str | None, str | None]] = []
        with Session(self.engine) as session:
            manifests = list(session.scalars(select(OAManifestItem).order_by(OAManifestItem.id)))
            keys = [row.oa_item_key for row in manifests]
            items = {
                row.oa_item_key: row for row in session.scalars(
                    select(OAItem).where(OAItem.source_channel == "done", OAItem.oa_item_key.in_(keys))
                )
            } if keys else {}
            def published_file_exists(relpath: str) -> bool:
                try:
                    return resolve_data_path(
                        self.settings.workspace_root, relpath,
                        allowed_prefixes=(relpath.split("/", 1)[0],),
                    ).is_file()
                except ValueError:
                    return False

            delivery = delivery_facts_map(session, list(items.values()), file_exists=published_file_exists)
            complete_items = {
                item.oa_item_key for item in items.values()
                if delivery[item.id]["status"] == "complete"
            }
            classifications = {
                row.oa_item_key: row.classification_status
                for row in session.scalars(select(ClassificationDecision).where(
                    ClassificationDecision.oa_item_key.in_(keys),
                    ClassificationDecision.is_current.is_(True),
                ))
            } if keys else {}
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
                if phase == "markdown" and relevant and relevant.stage == "classify":
                    classification = classifications.get(manifest.oa_item_key)
                    if classification == "needs_review":
                        counts["attention"] += 1
                        continue
                    if classification == "classified" and relevant.status in {"completed", "failed"}:
                        counts["markdown_requeued"] += 1
                        if apply:
                            actions.append(("requeue", phase, manifest.oa_item_key, relevant.id, item.id if item else None, None, None))
                        continue
                if relevant and relevant.status == "failed":
                    if relevant.recoverable or relevant.error_code in _LEGACY_RETRYABLE_ERRORS:
                        counts[f"{phase}_requeued"] += 1
                        if apply:
                            actions.append(("requeue", phase, manifest.oa_item_key, relevant.id, item.id if phase == "markdown" and item else None, None, None))
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
                            actions.append(("requeue", phase, manifest.oa_item_key, existing.id, item.id if phase == "markdown" and item else None, None, None))
                    elif existing.status == "failed":
                        counts["attention"] += 1
                    continue
                counts[f"{phase}_created"] += 1
                if apply:
                    actions.append(("create", phase, manifest.oa_item_key, None, None, stage, key))
            session.rollback()
        if apply:
            self._apply_actions(actions, counts)
        return DoneConvergenceReport(**counts)

    def _apply_actions(self, actions, counts: dict[str, int]) -> None:
        # The read snapshot is closed before this method starts. Each batch owns
        # a fresh, short SQLite write transaction, so workers can keep progressing.
        for offset in range(0, len(actions), 50):
            with Session(self.engine) as session:
                session.execute(text("BEGIN IMMEDIATE"))
                for kind, phase, oa_key, task_id, item_id, stage, idempotency_key in actions[offset:offset + 50]:
                    count_name = f"{phase}_{'created' if kind == 'create' else 'requeued'}"
                    manifest = session.scalar(select(OAManifestItem).where(OAManifestItem.oa_item_key == oa_key))
                    if manifest is None or manifest.processing_status == "skipped" or (manifest.matched_exclusion_keyword or "").strip():
                        counts[count_name] -= 1
                        continue
                    if kind == "requeue":
                        task = session.get(PipelineTask, task_id)
                        if task is None or task.status in {"queued", "running"}:
                            counts[count_name] -= 1
                            continue
                        item = session.get(OAItem, item_id) if item_id is not None else None
                        self._requeue(session, task, item=item)
                    else:
                        if session.scalar(select(PipelineTask.id).where(PipelineTask.idempotency_key == idempotency_key)) is not None:
                            counts[count_name] -= 1
                            continue
                        queue = "markdown_delivery" if phase == "markdown" else "realtime_done"
                        session.add(PipelineTask(
                            queue_name=queue, priority=QUEUE_PRIORITY[queue],
                            logical_item_key=oa_key, stage=stage,
                            idempotency_key=idempotency_key,
                            payload_json=json.dumps({"reason": "done_convergence"}),
                        ))
                session.commit()

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
        task.recoverable = True
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
