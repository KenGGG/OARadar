"""Reconciliation report for a frozen classification run."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.db.models import (
    ClassificationDecision,
    ClassificationEvidence,
    ClassificationRun,
    ClassificationRunItem,
)

from .schemas import PrivateClassificationConfig


@dataclass(frozen=True, slots=True)
class ClassificationRunReport:
    total: int
    excluded: int
    classification_target: int
    publishable: int
    integrity_blocked: int
    needs_review: int
    internal: int
    external: int
    initiator_roles: dict[str, tuple[str, ...]]
    unknown_initiators: tuple[str, ...]
    decision_sources: dict[str, int]
    needs_parse: int
    actual_parse_count: int
    expected_qwen_calls: int
    actual_qwen_calls: int
    conflicts: int
    unrecognized_issuers: int
    canonical_document_deduplications: int
    reconciled: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "excluded": self.excluded,
            "classification_target": self.classification_target,
            "publishable": self.publishable,
            "integrity_blocked": self.integrity_blocked,
            "needs_review": self.needs_review,
            "internal": self.internal,
            "external": self.external,
            "initiator_roles": self.initiator_roles,
            "unknown_initiators": self.unknown_initiators,
            "decision_sources": self.decision_sources,
            "needs_parse": self.needs_parse,
            "actual_parse_count": self.actual_parse_count,
            "expected_qwen_calls": self.expected_qwen_calls,
            "actual_qwen_calls": self.actual_qwen_calls,
            "conflicts": self.conflicts,
            "unrecognized_issuers": self.unrecognized_issuers,
            "canonical_document_deduplications": self.canonical_document_deduplications,
            "reconciled": self.reconciled,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> ClassificationRunReport:
        roles = value.get("initiator_roles", {})
        if not isinstance(roles, dict):
            raise TypeError("stored classification report has invalid initiator roles")
        return cls(
            total=int(value["total"]),
            excluded=int(value["excluded"]),
            classification_target=int(value["classification_target"]),
            publishable=int(value["publishable"]),
            integrity_blocked=int(value["integrity_blocked"]),
            needs_review=int(value["needs_review"]),
            internal=int(value["internal"]),
            external=int(value["external"]),
            initiator_roles={
                str(role): tuple(str(identifier) for identifier in identifiers)
                for role, identifiers in roles.items()
                if isinstance(identifiers, list)
            },
            unknown_initiators=tuple(
                str(identifier)
                for identifier in value["unknown_initiators"]  # type: ignore[index]
            ),
            decision_sources={
                str(source): int(count)
                for source, count in value["decision_sources"].items()  # type: ignore[index,union-attr]
            },
            needs_parse=int(value["needs_parse"]),
            actual_parse_count=int(value["actual_parse_count"]),
            expected_qwen_calls=int(value["expected_qwen_calls"]),
            actual_qwen_calls=int(value["actual_qwen_calls"]),
            conflicts=int(value["conflicts"]),
            unrecognized_issuers=int(value["unrecognized_issuers"]),
            canonical_document_deduplications=int(
                value["canonical_document_deduplications"]
            ),
            reconciled=bool(value["reconciled"]),
        )


def build_classification_run_report(
    session_factory: Callable[[], Session],
    run_id: str,
    config: PrivateClassificationConfig,
) -> ClassificationRunReport:
    with session_factory() as session:
        run = session.scalar(
            select(ClassificationRun).where(ClassificationRun.run_id == run_id)
        )
        if run is None:
            raise ValueError("classification run not found")
        if run.status == "completed":
            try:
                stored = json.loads(run.summary_json)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "completed classification run has invalid summary"
                ) from exc
            if not isinstance(stored, dict):
                raise ValueError("completed classification run has invalid summary")
            return ClassificationRunReport.from_dict(stored)
        rows = list(
            session.scalars(
                select(ClassificationRunItem).where(
                    ClassificationRunItem.classification_run_id == run.id
                )
            )
        )
        adopted_ids = [
            row.adopted_decision_id for row in rows if row.adopted_decision_id
        ]
        decisions = {
            decision.id: decision
            for decision in session.scalars(
                select(ClassificationDecision).where(
                    ClassificationDecision.id.in_(adopted_ids)
                )
            )
        }
        frozen = [(row, decisions.get(row.adopted_decision_id)) for row in rows]
        excluded = sum(row.inclusion_reason == "excluded" for row, _ in frozen)
        targets = [
            (row, decision)
            for row, decision in frozen
            if row.inclusion_reason == "target"
        ]
        needs_review = sum(
            decision is not None and decision.classification_status == "needs_review"
            for _, decision in targets
        )
        integrity_blocked = sum(
            decision is not None
            and decision.classification_status == "classified"
            and decision.content_integrity_status
            not in {"ok", "no_attachment_confirmed"}
            for _, decision in targets
        )
        publishable = sum(
            decision is not None
            and decision.classification_status == "classified"
            and decision.content_integrity_status in {"ok", "no_attachment_confirmed"}
            for _, decision in targets
        )
        classified = [
            decision
            for _, decision in targets
            if decision is not None and decision.classification_status == "classified"
        ]
        role_members: dict[str, list[str]] = defaultdict(list)
        for identifier, profile in config.initiators.items():
            role_members[profile.role].append(identifier)
        roles = {
            role: tuple(sorted(role_members.get(role, [])))
            for role in ("internal", "external", "mixed", "system", "unknown")
        }
        sources = Counter(
            decision.decision_source for _, decision in targets if decision is not None
        )
        reasons = [
            _decode_reason(decision.classification_reason_json)
            for _, decision in targets
            if decision is not None
        ]
        evidence = (
            list(
                session.scalars(
                    select(ClassificationEvidence).where(
                        ClassificationEvidence.classification_decision_id.in_(
                            [
                                decision.id
                                for _, decision in targets
                                if decision is not None
                            ]
                        )
                    )
                )
            )
            if targets
            else []
        )
        documents = [
            (decision.canonical_issuer, decision.document_number)
            for decision in classified
            if decision.canonical_issuer and decision.document_number
        ]
        unknown = tuple(sorted(roles["unknown"]))
        reconciled = (
            len(rows) == run.target_count + run.excluded_count
            and len(targets) == run.target_count
            and all(decision is not None for _, decision in frozen)
            and len(rows) == excluded + publishable + integrity_blocked + needs_review
            and len(targets) == publishable + integrity_blocked + needs_review
        )
        return ClassificationRunReport(
            total=len(rows),
            excluded=excluded,
            classification_target=len(targets),
            publishable=publishable,
            integrity_blocked=integrity_blocked,
            needs_review=needs_review,
            internal=sum(
                decision.content_origin == "internal" for decision in classified
            ),
            external=sum(
                decision.content_origin == "external" for decision in classified
            ),
            initiator_roles=roles,
            unknown_initiators=unknown,
            decision_sources=dict(sorted(sources.items())),
            needs_parse=sum(
                reason.get("escalation_action") == "parse_attachment"
                for reason in reasons
            ),
            actual_parse_count=len(
                {
                    entry.parse_artifact_id
                    for entry in evidence
                    if entry.parse_artifact_id is not None
                }
            ),
            expected_qwen_calls=0,
            actual_qwen_calls=sum(
                decision.decision_source == "local_qwen"
                for _, decision in targets
                if decision is not None
            ),
            conflicts=(
                sum(bool(reason.get("conflict_codes")) for reason in reasons)
                + sum(
                    row.last_error_code == "manual_lock_policy_conflict"
                    for row, _ in frozen
                )
            ),
            unrecognized_issuers=sum(
                _has_unrecognized_issuer(entry.value_json) for entry in evidence
            ),
            canonical_document_deduplications=len(documents) - len(set(documents)),
            reconciled=reconciled,
        )


def _decode_reason(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _has_unrecognized_issuer(raw: str) -> bool:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return False
    return (
        isinstance(value, dict)
        and value.get("issuer_resolution_status") == "unrecognized"
    )
