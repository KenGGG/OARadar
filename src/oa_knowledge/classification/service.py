"""Durable, metadata-first classification run orchestration.

This module deliberately does not parse attachments or call a model.  Those
are later escalation stages; an unresolved metadata result is durable here so
it can be reviewed or resumed without losing its place.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oa_knowledge.db.models import (
    ClassificationDecision,
    ClassificationEvidence,
    ClassificationRun,
    ClassificationRunItem,
    OAItem,
    OAManifestItem,
)

from .evidence import AttachmentCandidate, ClassificationItem, Evidence, RuleOutcome
from .metadata_rules import classify_from_metadata, collect_metadata_evidence
from .schemas import PrivateClassificationConfig


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class CreateClassificationRun:
    run_id: str
    run_kind: str
    manifest_sha256: str
    exclusion_policy_sha256: str
    rule_version: str
    schema_version: str
    prompt_version: str
    model_name: str
    private_config_sha256: str


@dataclass(frozen=True, slots=True)
class ClassificationRunRef:
    run_id: str
    database_id: int
    total_count: int
    target_count: int
    excluded_count: int


@dataclass(frozen=True, slots=True)
class ClassificationProgress:
    total: int
    queued: int
    decided: int
    failed: int


@dataclass(frozen=True, slots=True)
class ManualDecisionCommand:
    run_id: str
    oa_item_key: str
    actor: str
    reason: str
    classification_status: str
    content_origin: str | None
    business_category: str | None
    canonical_issuer: str | None
    flow_type: str | None
    initiator_type: str
    issuer: str | None = None
    document_number: str | None = None
    document_type: str | None = None


@dataclass(frozen=True, slots=True)
class DecisionRef:
    decision_id: int
    version: int


class ClassificationService:
    """Owns the durable state transitions for one classification run."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        config: PrivateClassificationConfig,
        *,
        outcome_hook: Callable[
            [ClassificationItem, list[Evidence], RuleOutcome], RuleOutcome
        ]
        | None = None,
    ) -> None:
        self._sessions = session_factory
        self._config = config
        self._outcome_hook = outcome_hook

    def create_run(self, request: CreateClassificationRun) -> ClassificationRunRef:
        with self._sessions.begin() as session:
            manifests = list(
                session.scalars(
                    select(OAManifestItem).order_by(
                        OAManifestItem.list_page,
                        OAManifestItem.list_ordinal,
                        OAManifestItem.oa_item_key,
                    )
                )
            )
            membership = [
                {
                    "oa_item_key": row.oa_item_key,
                    "inclusion_reason": self._inclusion_reason(row),
                }
                for row in manifests
            ]
            signature = _sha256(
                {"request": asdict(request) | {"run_id": ""}, "membership": membership}
            )
            existing = session.scalar(
                select(ClassificationRun).where(
                    ClassificationRun.run_id == request.run_id
                )
            )
            if existing is not None:
                if existing.input_signature != signature:
                    raise ValueError(
                        "classification run id already exists with different inputs"
                    )
                return ClassificationRunRef(
                    run_id=existing.run_id,
                    database_id=existing.id,
                    total_count=existing.target_count + existing.excluded_count,
                    target_count=existing.target_count,
                    excluded_count=existing.excluded_count,
                )

            excluded_count = sum(
                row["inclusion_reason"] == "excluded" for row in membership
            )
            run = ClassificationRun(
                run_id=request.run_id,
                run_kind=request.run_kind,
                status="created",
                input_signature=signature,
                manifest_sha256=request.manifest_sha256,
                exclusion_policy_sha256=request.exclusion_policy_sha256,
                rule_version=request.rule_version,
                schema_version=request.schema_version,
                prompt_version=request.prompt_version,
                model_name=request.model_name,
                private_config_sha256=request.private_config_sha256,
                target_count=len(membership) - excluded_count,
                excluded_count=excluded_count,
            )
            session.add(run)
            session.flush()
            session.add_all(
                ClassificationRunItem(
                    classification_run_id=run.id,
                    oa_item_key=row["oa_item_key"],
                    inclusion_reason=row["inclusion_reason"],
                )
                for row in membership
            )
            return ClassificationRunRef(
                run_id=run.run_id,
                database_id=run.id,
                total_count=len(membership),
                target_count=run.target_count,
                excluded_count=run.excluded_count,
            )

    def process_next(self, run_id: str, *, limit: int = 1) -> ClassificationProgress:
        if limit < 1:
            raise ValueError("limit must be positive")
        for _ in range(limit):
            claimed = self._claim_next(run_id)
            if claimed is None:
                break
            run_item_id, item_key = claimed
            try:
                self._decide_claimed(run_id, run_item_id, item_key)
            except Exception:  # noqa: BLE001 - a worker boundary must checkpoint all failures
                self._mark_failed(run_item_id)
        return self.progress(run_id)

    def resume(self, run_id: str) -> ClassificationProgress:
        with self._sessions.begin() as session:
            run = self._run(session, run_id)
            session.execute(
                ClassificationRunItem.__table__.update()
                .where(
                    ClassificationRunItem.classification_run_id == run.id,
                    ClassificationRunItem.stage == "failed",
                )
                .values(stage="queued", last_error_code=None, last_error_detail=None)
            )
        return self.process_next(run_id, limit=self._run_total(run_id))

    def progress(self, run_id: str) -> ClassificationProgress:
        with self._sessions() as session:
            run = self._run(session, run_id)
            stages = dict(
                session.execute(
                    select(ClassificationRunItem.stage, func.count())
                    .where(ClassificationRunItem.classification_run_id == run.id)
                    .group_by(ClassificationRunItem.stage)
                ).all()
            )
            return ClassificationProgress(
                total=sum(stages.values()),
                queued=stages.get("queued", 0),
                decided=stages.get("decided", 0),
                failed=stages.get("failed", 0),
            )

    def set_manual_decision(self, command: ManualDecisionCommand) -> DecisionRef:
        if not command.actor.strip() or not command.reason.strip():
            raise ValueError("manual actor and reason are required")
        with self._sessions.begin() as session:
            run = self._run(session, command.run_id)
            if run.status == "completed":
                raise ValueError("completed classification runs are immutable")
            run_item = session.scalar(
                select(ClassificationRunItem).where(
                    ClassificationRunItem.classification_run_id == run.id,
                    ClassificationRunItem.oa_item_key == command.oa_item_key,
                )
            )
            if run_item is None:
                raise ValueError("OA item is not frozen in this classification run")
            current = self._current(session, command.oa_item_key)
            integrity = current.content_integrity_status if current else "not_checked"
            normalized_title = (
                current.normalized_title
                if current
                else self._title_for(session, command.oa_item_key)
            )
            version = self._next_version(session, command.oa_item_key)
            decision = ClassificationDecision(
                classification_run_id=run.id,
                oa_item_key=command.oa_item_key,
                version=version,
                is_current=False,
                decision_input_sha256=_sha256(
                    {"manual": asdict(command), "version": version}
                ),
                decision_source="manual",
                classification_status=command.classification_status,
                content_integrity_status=integrity,
                content_origin=command.content_origin,
                flow_type=command.flow_type,
                initiator=current.initiator if current else None,
                initiator_type=command.initiator_type,
                relay_from=current.relay_from if current else None,
                transfer_chain_json=current.transfer_chain_json if current else "[]",
                issuer=command.issuer,
                canonical_issuer=command.canonical_issuer,
                business_category=command.business_category,
                document_number=command.document_number,
                document_type=command.document_type,
                normalized_title=normalized_title,
                classification_confidence=1.0,
                classification_reason_json=_canonical_json(
                    {"reason": command.reason, "manual": True}
                ),
                rule_version=run.rule_version,
                private_config_sha256=run.private_config_sha256,
                manual_locked=True,
                actor=command.actor.strip(),
                supersedes_decision_id=current.id if current else None,
            )
            self._swap_current(session, current, decision)
            run_item.adopted_decision_id = decision.id
            run_item.stage = "decided"
            self._mirror_compatibility(session, command.oa_item_key, decision)
            return DecisionRef(decision.id, decision.version)

    def complete(self, run_id: str):
        from .reporting import build_classification_run_report

        with self._sessions() as session:
            run = self._run(session, run_id)
            if run.status == "completed":
                return build_classification_run_report(
                    self._sessions, run_id, self._config
                )
        progress = self.progress(run_id)
        if progress.queued or progress.failed or progress.decided != progress.total:
            raise ValueError("classification run has unfinished items")
        report = build_classification_run_report(self._sessions, run_id, self._config)
        if not report.reconciled:
            raise ValueError("classification run does not reconcile")
        with self._sessions.begin() as session:
            run = self._run(session, run_id)
            run.status = "completed"
            run.finished_at = _now()
            run.summary_json = _canonical_json(report.to_dict())
        return report

    @staticmethod
    def _inclusion_reason(manifest: OAManifestItem) -> str:
        if (
            manifest.processing_status == "skipped"
            or (manifest.matched_exclusion_keyword or "").strip()
        ):
            return "excluded"
        return "target"

    def _claim_next(self, run_id: str) -> tuple[int, str] | None:
        with self._sessions.begin() as session:
            run = self._run(session, run_id)
            if run.status == "completed":
                return None
            if run.status == "created":
                run.status = "running"
                run.started_at = _now()
            candidate = session.scalar(
                select(ClassificationRunItem)
                .where(
                    ClassificationRunItem.classification_run_id == run.id,
                    ClassificationRunItem.stage == "queued",
                )
                .order_by(ClassificationRunItem.id)
                .limit(1)
            )
            if candidate is None:
                return None
            claimed = session.execute(
                ClassificationRunItem.__table__.update()
                .where(
                    ClassificationRunItem.id == candidate.id,
                    ClassificationRunItem.stage == "queued",
                )
                .values(stage="metadata", attempts=ClassificationRunItem.attempts + 1)
            )
            if claimed.rowcount != 1:
                return None
            return candidate.id, candidate.oa_item_key

    def _decide_claimed(self, run_id: str, run_item_id: int, item_key: str) -> None:
        with self._sessions.begin() as session:
            run = self._run(session, run_id)
            run_item = session.get(ClassificationRunItem, run_item_id)
            if run_item is None or run_item.stage != "metadata":
                return
            current = self._current(session, item_key)
            if current is not None and current.manual_locked:
                run_item.adopted_decision_id = current.id
                if (
                    run_item.inclusion_reason == "excluded"
                    and current.classification_status != "excluded"
                ):
                    run_item.last_error_code = "manual_lock_policy_conflict"
                run_item.stage = "decided"
                return
            if run_item.inclusion_reason == "excluded":
                decision = self._excluded_decision(session, run, item_key, current)
                if decision is not None:
                    self._swap_current(session, current, decision)
                    run_item.adopted_decision_id = decision.id
                elif current is not None:
                    run_item.adopted_decision_id = current.id
                run_item.stage = "decided"
                return

            item = self._classification_item(session, item_key)
            evidence = collect_metadata_evidence(item, self._config)
            outcome = classify_from_metadata(evidence)
            if self._outcome_hook is not None:
                outcome = self._outcome_hook(item, evidence, outcome)
            input_sha = self._decision_input_sha(
                session, item, run_item.inclusion_reason, run
            )
            if current is not None and current.decision_input_sha256 == input_sha:
                run_item.adopted_decision_id = current.id
                run_item.stage = "decided"
                return
            decision = self._automatic_decision(
                session, run, item, evidence, outcome, input_sha, current
            )
            self._swap_current(session, current, decision)
            self._persist_evidence(session, decision, evidence, item_key)
            run_item.adopted_decision_id = decision.id
            self._mirror_compatibility(session, item_key, decision)
            run_item.stage = "decided"

    def _automatic_decision(
        self,
        session: Session,
        run: ClassificationRun,
        item: ClassificationItem,
        evidence: list[Evidence],
        outcome: RuleOutcome,
        input_sha: str,
        current: ClassificationDecision | None,
    ) -> ClassificationDecision:
        return ClassificationDecision(
            classification_run_id=run.id,
            oa_item_key=item.item_key,
            version=self._next_version_for_current(current),
            is_current=False,
            decision_input_sha256=input_sha,
            decision_source=outcome.decision_source or "metadata_rule",
            classification_status=outcome.classification_status,
            content_integrity_status=self._integrity_status(session, item.item_key),
            content_origin=outcome.content_origin,
            flow_type=outcome.flow_type,
            initiator=item.initiator,
            initiator_type=outcome.initiator_role or "unknown",
            relay_from=outcome.relay_from,
            transfer_chain_json=_canonical_json(
                [asdict(hop) for hop in outcome.transfer_chain]
            ),
            issuer=outcome.issuer,
            canonical_issuer=outcome.canonical_issuer,
            business_category=outcome.business_category,
            document_number=outcome.document_number,
            document_type=outcome.document_type,
            normalized_title=outcome.normalized_title,
            classification_confidence=outcome.confidence,
            classification_reason_json=_canonical_json(
                {
                    "escalation_action": outcome.escalation_action,
                    "conflict_codes": list(outcome.conflict_codes),
                    "evidence_codes": [entry.code for entry in evidence],
                }
            ),
            rule_version=run.rule_version,
            private_config_sha256=run.private_config_sha256,
            manual_locked=False,
            supersedes_decision_id=current.id if current else None,
        )

    def _excluded_decision(
        self,
        session: Session,
        run: ClassificationRun,
        item_key: str,
        current: ClassificationDecision | None,
    ) -> ClassificationDecision | None:
        item = self._classification_item(session, item_key)
        input_sha = self._decision_input_sha(session, item, "excluded", run)
        if current is not None and current.decision_input_sha256 == input_sha:
            return None
        return ClassificationDecision(
            classification_run_id=run.id,
            oa_item_key=item_key,
            version=self._next_version_for_current(current),
            is_current=False,
            decision_input_sha256=input_sha,
            decision_source="metadata_rule",
            classification_status="excluded",
            content_integrity_status=self._integrity_status(session, item_key),
            content_origin=None,
            flow_type=None,
            initiator=item.initiator,
            initiator_type="unknown",
            relay_from=None,
            transfer_chain_json="[]",
            issuer=None,
            canonical_issuer=None,
            business_category=None,
            document_number=None,
            document_type=None,
            normalized_title=item.title,
            classification_confidence=1.0,
            classification_reason_json=_canonical_json(
                {"exclusion": "frozen_manifest_policy"}
            ),
            rule_version=run.rule_version,
            private_config_sha256=run.private_config_sha256,
            manual_locked=False,
            supersedes_decision_id=current.id if current else None,
        )

    def _swap_current(
        self,
        session: Session,
        current: ClassificationDecision | None,
        decision: ClassificationDecision,
    ) -> None:
        if current is not None:
            current.is_current = False
            session.flush()
        decision.is_current = True
        session.add(decision)
        session.flush()

    def _classification_item(
        self, session: Session, item_key: str
    ) -> ClassificationItem:
        manifest = session.scalar(
            select(OAManifestItem).where(OAManifestItem.oa_item_key == item_key)
        )
        item = session.scalar(select(OAItem).where(OAItem.oa_item_key == item_key))
        if manifest is None and item is None:
            raise ValueError("frozen OA item no longer exists")
        owner = manifest or item
        files = list(item.files) if item is not None else []
        return ClassificationItem(
            item_key=item_key,
            title=owner.title,
            initiator=owner.sender or "",
            document_number=item.document_number if item is not None else None,
            attachments=tuple(
                AttachmentCandidate(
                    attachment_key=file.attachment_key,
                    filename=file.original_name,
                    source_file_id=file.id,
                )
                for file in sorted(files, key=lambda row: (row.attachment_key, row.id))
            ),
        )

    def _decision_input_sha(
        self,
        session: Session,
        item: ClassificationItem,
        inclusion_reason: str,
        run: ClassificationRun,
    ) -> str:
        manifest = session.scalar(
            select(OAManifestItem).where(OAManifestItem.oa_item_key == item.item_key)
        )
        owner = session.scalar(
            select(OAItem).where(OAItem.oa_item_key == item.item_key)
        )
        attachment_inventory = []
        if owner is not None:
            attachment_inventory = [
                {
                    "attachment_key": row.attachment_key,
                    "file_role": row.file_role,
                    "source_container_key": row.source_container_key,
                    "parent_file_id": row.parent_file_id,
                    "depth": row.depth,
                    "sha256": row.sha256.lower() if row.sha256 else None,
                    "size_bytes": row.size_bytes,
                    "download_status": row.download_status,
                }
                for row in sorted(
                    owner.files,
                    key=lambda row: (
                        row.source_container_key,
                        row.attachment_key,
                        row.file_role,
                        row.id,
                    ),
                )
            ]
        return _sha256(
            {
                "item": asdict(item),
                "inclusion_reason": inclusion_reason,
                "manifest": {
                    "title": manifest.title if manifest else None,
                    "sender": manifest.sender if manifest else None,
                    "processing_status": manifest.processing_status
                    if manifest
                    else None,
                    "no_attachment_confirmed": manifest.no_attachment_confirmed
                    if manifest
                    else None,
                    "matched_exclusion_keyword": manifest.matched_exclusion_keyword
                    if manifest
                    else None,
                    "completed_at": manifest.completed_at.isoformat()
                    if manifest and manifest.completed_at
                    else None,
                },
                "item_metadata": {
                    "title": owner.title if owner else None,
                    "sender": owner.sender if owner else None,
                    "document_number": owner.document_number if owner else None,
                    "completed_at": owner.completed_at.isoformat()
                    if owner and owner.completed_at
                    else None,
                },
                "attachment_inventory": attachment_inventory,
                "rule_version": run.rule_version,
                "schema_version": run.schema_version,
                "prompt_version": run.prompt_version,
                "private_config_sha256": run.private_config_sha256,
            }
        )

    @staticmethod
    def _integrity_status(session: Session, item_key: str) -> str:
        manifest = session.scalar(
            select(OAManifestItem).where(OAManifestItem.oa_item_key == item_key)
        )
        if manifest is None:
            return "not_checked"
        if manifest.no_attachment_confirmed:
            return "no_attachment_confirmed"
        if manifest.processing_status in {"download_failed", "failed"}:
            return "download_failed"
        return "ok" if manifest.processing_status == "downloaded" else "not_checked"

    @staticmethod
    def _persist_evidence(
        session: Session,
        decision: ClassificationDecision,
        evidence: list[Evidence],
        item_key: str,
    ) -> None:
        for sequence, entry in enumerate(evidence, start=1):
            value = {
                key: value
                for key, value in asdict(entry).items()
                if value is not None and key not in {"normalized_title"}
            }
            session.add(
                ClassificationEvidence(
                    classification_decision_id=decision.id,
                    sequence=sequence,
                    evidence_type=entry.code,
                    evidence_scope=entry.evidence_scope,
                    value_json=_canonical_json(value),
                    confidence=entry.confidence,
                    source_file_id=entry.source_file_id,
                )
            )

    @staticmethod
    def _next_version_for_current(current: ClassificationDecision | None) -> int:
        return 1 if current is None else current.version + 1

    @staticmethod
    def _next_version(session: Session, item_key: str) -> int:
        maximum = session.scalar(
            select(func.max(ClassificationDecision.version)).where(
                ClassificationDecision.oa_item_key == item_key
            )
        )
        return (maximum or 0) + 1

    @staticmethod
    def _current(session: Session, item_key: str) -> ClassificationDecision | None:
        return session.scalar(
            select(ClassificationDecision).where(
                ClassificationDecision.oa_item_key == item_key,
                ClassificationDecision.is_current.is_(True),
            )
        )

    @staticmethod
    def _run(session: Session, run_id: str) -> ClassificationRun:
        run = session.scalar(
            select(ClassificationRun).where(ClassificationRun.run_id == run_id)
        )
        if run is None:
            raise ValueError("classification run not found")
        return run

    def _run_total(self, run_id: str) -> int:
        with self._sessions() as session:
            run = self._run(session, run_id)
            return run.target_count + run.excluded_count

    @staticmethod
    def _title_for(session: Session, item_key: str) -> str:
        item = session.scalar(select(OAItem).where(OAItem.oa_item_key == item_key))
        if item is not None:
            return item.title
        manifest = session.scalar(
            select(OAManifestItem).where(OAManifestItem.oa_item_key == item_key)
        )
        return manifest.title if manifest is not None else ""

    def _mark_failed(self, run_item_id: int) -> None:
        with self._sessions.begin() as session:
            item = session.get(ClassificationRunItem, run_item_id)
            if item is not None and item.stage == "metadata":
                item.stage = "failed"
                item.last_error_code = "classification_attempt_failed"
                item.last_error_detail = None

    @staticmethod
    def _mirror_compatibility(
        session: Session, item_key: str, decision: ClassificationDecision
    ) -> None:
        item = session.scalar(select(OAItem).where(OAItem.oa_item_key == item_key))
        if item is None or decision.classification_status != "classified":
            return
        item.source_type = decision.content_origin
        item.internal_category = decision.business_category
        item.external_issuer = decision.canonical_issuer
        item.classification_version = f"decision-v{decision.version}"
