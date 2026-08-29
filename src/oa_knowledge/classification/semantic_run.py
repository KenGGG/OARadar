"""Durable, scoped semantic review over frozen current classifications."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oa_knowledge.db.models import (
    ClassificationDecision,
    ClassificationEvidence,
    ClassificationRun,
    ClassificationRunItem,
    OAManifestItem,
)

from .agnes_eligibility import AgnesEligibility
from .semantic_classifier import (
    SemanticClassificationResult,
    SemanticPackage,
)


@dataclass(frozen=True, slots=True)
class SemanticRunRef:
    run_id: str
    target_count: int
    excluded_count: int


@dataclass(frozen=True, slots=True)
class SemanticRunProgress:
    total: int
    queued: int
    decided: int
    failed: int


class _Classifier(Protocol):
    def classify(
        self, package: SemanticPackage, eligibility: AgnesEligibility
    ) -> SemanticClassificationResult: ...


def semantic_target_keys(session: Session) -> tuple[str, ...]:
    """Freeze only Gate-0-admitted current classifications for semantic review."""
    rows = session.scalars(
        select(ClassificationDecision.oa_item_key)
        .join(OAManifestItem, OAManifestItem.oa_item_key == ClassificationDecision.oa_item_key)
        .where(
            ClassificationDecision.is_current.is_(True),
            ClassificationDecision.classification_status.in_(("classified", "needs_review")),
            OAManifestItem.matched_exclusion_keyword.is_(None),
        )
        .order_by(ClassificationDecision.oa_item_key)
    )
    return tuple(rows)


class SemanticReviewService:
    """Adopt semantic outcomes only under the freeze-safe confidence thresholds."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        classifier: _Classifier,
        package_loader: Callable[[str], tuple[SemanticPackage, AgnesEligibility]],
    ) -> None:
        self._sessions = session_factory
        self._classifier = classifier
        self._package_loader = package_loader

    def create_run(
        self, run_id: str, target_keys: tuple[str, ...], *, private_config_sha256: str
    ) -> SemanticRunRef:
        if not target_keys or len(set(target_keys)) != len(target_keys):
            raise ValueError("semantic run requires unique target keys")
        with self._sessions.begin() as session:
            existing = session.scalar(select(ClassificationRun).where(ClassificationRun.run_id == run_id))
            if existing is not None:
                return SemanticRunRef(existing.run_id, existing.target_count, existing.excluded_count)
            manifests = {
                row.oa_item_key: row
                for row in session.scalars(select(OAManifestItem).where(OAManifestItem.oa_item_key.in_(target_keys)))
            }
            if set(target_keys) != set(manifests):
                raise ValueError("semantic run target key is unknown")
            if any(row.matched_exclusion_keyword for row in manifests.values()):
                raise ValueError("excluded OA cannot enter semantic run")
            signature = hashlib.sha256("\n".join(sorted(target_keys)).encode()).hexdigest()
            run = ClassificationRun(
                run_id=run_id, run_kind="incremental", status="created", input_signature=signature,
                manifest_sha256=signature, exclusion_policy_sha256=signature, rule_version="semantic-v2",
                schema_version="classification-v1", prompt_version="agnes-classifier-v1.1",
                model_name="agnes-2.0-flash+local-qwen", private_config_sha256=private_config_sha256,
                target_count=len(target_keys), excluded_count=0,
            )
            session.add(run)
            session.flush()
            session.add_all(
                ClassificationRunItem(classification_run_id=run.id, oa_item_key=key, inclusion_reason="semantic_review")
                for key in target_keys
            )
            return SemanticRunRef(run_id, len(target_keys), 0)

    def process_next(self, run_id: str, *, limit: int = 1) -> SemanticRunProgress:
        for _ in range(limit):
            claimed = self._claim(run_id)
            if claimed is None:
                break
            item_id, key = claimed
            try:
                self._process_claimed(run_id, item_id, key)
            except Exception as exc:  # noqa: BLE001 - worker boundary persists retryable item failure
                self._fail(item_id, type(exc).__name__)
        return self.progress(run_id)

    def recover_interrupted(self, run_id: str) -> int:
        """Return in-flight items to the durable queue after a worker dies.

        No decision is created or adopted here: an item in ``content`` has not
        completed model processing and can therefore be safely retried.
        """
        with self._sessions.begin() as session:
            run = self._run(session, run_id)
            items = list(session.scalars(select(ClassificationRunItem).where(
                ClassificationRunItem.classification_run_id == run.id,
                ClassificationRunItem.stage == "content",
            )))
            for item in items:
                item.stage = "queued"
                item.last_error_code = "worker_interrupted"
                item.last_error_detail = "requeued after interrupted semantic worker"
            return len(items)

    def progress(self, run_id: str) -> SemanticRunProgress:
        with self._sessions.begin() as session:
            run = self._run(session, run_id)
            stages = dict(session.execute(
                select(ClassificationRunItem.stage, func.count()).where(
                    ClassificationRunItem.classification_run_id == run.id
                ).group_by(ClassificationRunItem.stage)
            ).all())
            queued = stages.get("queued", 0) + stages.get("content", 0)
            result = SemanticRunProgress(run.target_count, queued, stages.get("decided", 0), stages.get("failed", 0))
            if result.queued == 0 and result.failed == 0:
                run.status = "completed"
                run.finished_at = datetime.now(UTC)
            elif run.status == "created":
                run.status = "running"
                run.started_at = datetime.now(UTC)
            return result

    def _claim(self, run_id: str) -> tuple[int, str] | None:
        with self._sessions.begin() as session:
            run = self._run(session, run_id)
            item = session.scalar(select(ClassificationRunItem).where(
                ClassificationRunItem.classification_run_id == run.id,
                ClassificationRunItem.stage == "queued",
            ).order_by(ClassificationRunItem.id).limit(1))
            if item is None:
                return None
            item.stage = "content"
            item.attempts += 1
            run.status = "running"
            run.started_at = run.started_at or datetime.now(UTC)
            return item.id, item.oa_item_key

    def _process_claimed(self, run_id: str, item_id: int, key: str) -> None:
        with self._sessions.begin() as session:
            run = self._run(session, run_id)
            item = session.get(ClassificationRunItem, item_id)
            current = self._current(session, key)
            if item is None or item.stage != "content" or current is None:
                raise ValueError("semantic run item/current decision missing")
            if current.manual_locked:
                item.adopted_decision_id = current.id
                item.stage = "decided"
                return
        package, eligibility = self._package_loader(key)
        # A semantic provider must never receive an OA whose usable content has
        # not been assembled locally.  The caller may later create a ParseArtifact
        # through the existing FormatRouter, then resume this scoped run.
        if not package.parsed_attachments:
            with self._sessions.begin() as session:
                item = session.get(ClassificationRunItem, item_id)
                current = self._current(session, key)
                if item is None or current is None or item.stage != "content":
                    raise ValueError("semantic run state changed before content gate")
                item.adopted_decision_id = current.id
                item.last_error_code = "no_parseable_content"
                item.last_error_detail = "no valid local ParseArtifact"
                item.stage = "decided"
            return
        result = self._classifier.classify(package, eligibility)
        with self._sessions.begin() as session:
            run = self._run(session, run_id)
            item = session.get(ClassificationRunItem, item_id)
            current = self._current(session, key)
            if item is None or current is None or item.stage != "content":
                raise ValueError("semantic run state changed during classification")
            item.last_error_code = None
            item.last_error_detail = self._audit_detail(result)
            if not self._adoptable(current, result):
                item.adopted_decision_id = current.id
                item.stage = "decided"
                return
            decision = self._decision(session, run, current, result)
            current.is_current = False
            session.flush()
            decision.is_current = True
            session.add(decision)
            session.flush()
            self._evidence(session, decision, result)
            item.adopted_decision_id = decision.id
            item.stage = "decided"

    @staticmethod
    def _adoptable(current: ClassificationDecision, result: SemanticClassificationResult) -> bool:
        outcome = result.outcome
        if outcome is None:
            return False
        if current.classification_status == "needs_review":
            return outcome.classification_status == "classified" and outcome.confidence >= 0.90
        return (
            outcome.review_current == "replace"
            and outcome.classification_status == "classified"
            and outcome.confidence >= 0.95
            and bool(outcome.evidence)
        )

    @staticmethod
    def _decision(session: Session, run: ClassificationRun, current: ClassificationDecision, result: SemanticClassificationResult) -> ClassificationDecision:
        outcome = result.outcome
        assert outcome is not None
        version = (session.scalar(select(func.max(ClassificationDecision.version)).where(
            ClassificationDecision.oa_item_key == current.oa_item_key
        )) or 0) + 1
        return ClassificationDecision(
            classification_run_id=run.id, oa_item_key=current.oa_item_key, version=version, is_current=False,
            decision_input_sha256=result.input_sha256, decision_source=result.provider,
            classification_status=outcome.classification_status, content_integrity_status=current.content_integrity_status,
            content_origin=outcome.content_origin, flow_type=outcome.flow_type, initiator=current.initiator,
            initiator_type=current.initiator_type, relay_from=current.relay_from,
            transfer_chain_json=current.transfer_chain_json, issuer=outcome.canonical_issuer,
            canonical_issuer=outcome.canonical_issuer, business_category=outcome.business_category,
            document_number=current.document_number, document_type=outcome.document_type,
            normalized_title=current.normalized_title, classification_confidence=outcome.confidence,
            classification_reason_json=json.dumps({"provider": result.provider, "model": result.model, "prompt_version": run.prompt_version, "input_sha256": result.input_sha256, "eligibility_reason": result.eligibility_reason, "reason": outcome.reason, "conflict_codes": []}, ensure_ascii=False, sort_keys=True),
            rule_version=run.rule_version, private_config_sha256=run.private_config_sha256,
            manual_locked=False, supersedes_decision_id=current.id,
        )

    @staticmethod
    def _evidence(session: Session, decision: ClassificationDecision, result: SemanticClassificationResult) -> None:
        assert result.outcome is not None
        for sequence, entry in enumerate(result.outcome.evidence, start=1):
            session.add(ClassificationEvidence(
                classification_decision_id=decision.id, sequence=sequence, evidence_type=entry.type,
                evidence_scope="attachment", value_json=entry.model_dump_json(), confidence=result.outcome.confidence,
            ))

    @staticmethod
    def _audit_detail(result: SemanticClassificationResult) -> str:
        """Persist model-call audit metadata without prompt or OA body content."""
        return json.dumps(
            {
                "provider": result.provider,
                "model": result.model,
                "prompt_version": "agnes-classifier-v1.1",
                "input_sha256": result.input_sha256,
                "eligibility_reason": result.eligibility_reason,
                "confidence": result.outcome.confidence if result.outcome else None,
                "result": result.outcome.classification_status if result.outcome else "rejected",
                "rejection_code": result.rejection_code,
                "cache_hit": result.cache_hit,
                "timestamp": datetime.now(UTC).isoformat(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def _fail(self, item_id: int, detail: str) -> None:
        with self._sessions.begin() as session:
            item = session.get(ClassificationRunItem, item_id)
            if item is not None and item.stage == "content":
                item.stage = "failed"
                item.last_error_code = "semantic_attempt_failed"
                item.last_error_detail = detail

    @staticmethod
    def _current(session: Session, key: str) -> ClassificationDecision | None:
        return session.scalar(select(ClassificationDecision).where(
            ClassificationDecision.oa_item_key == key, ClassificationDecision.is_current.is_(True)
        ))

    @staticmethod
    def _run(session: Session, run_id: str) -> ClassificationRun:
        run = session.scalar(select(ClassificationRun).where(ClassificationRun.run_id == run_id))
        if run is None:
            raise ValueError("semantic run not found")
        return run
