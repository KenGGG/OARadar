"""Read-only reconciliation artifacts for a completed semantic review run."""

from __future__ import annotations

import csv
import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.db.models import (
    ClassificationDecision,
    ClassificationRun,
    ClassificationRunItem,
)


def write_semantic_run_reports(
    session_factory: Callable[[], Session], run_id: str, output_root: Path
) -> dict[str, object]:
    """Write aggregate-only audit and remaining-review artifacts for one run.

    The function intentionally consumes the run's adopted decision IDs rather
    than whatever may later become globally current. It writes no DB state and
    never includes parsed OA content in reports.
    """
    output_root.mkdir(parents=True, exist_ok=True)
    with session_factory() as session:
        run = session.scalar(select(ClassificationRun).where(ClassificationRun.run_id == run_id))
        if run is None:
            raise ValueError("semantic run not found")
        items = list(session.scalars(select(ClassificationRunItem).where(
            ClassificationRunItem.classification_run_id == run.id
        ).order_by(ClassificationRunItem.id)))
        ids = [item.adopted_decision_id for item in items if item.adopted_decision_id]
        decisions = {
            decision.id: decision
            for decision in session.scalars(select(ClassificationDecision).where(
                ClassificationDecision.id.in_(ids)
            ))
        } if ids else {}
        prior_ids = [
            decision.supersedes_decision_id
            for decision in decisions.values()
            if decision.classification_run_id == run.id and decision.supersedes_decision_id
        ]
        prior = {
            decision.id: decision
            for decision in session.scalars(select(ClassificationDecision).where(
                ClassificationDecision.id.in_(prior_ids)
            ))
        } if prior_ids else {}

        stages = Counter(item.stage for item in items)
        models: Counter[str] = Counter()
        result_counts: Counter[str] = Counter()
        rejections: Counter[str] = Counter()
        cache_hits = retries = 0
        for item in items:
            audit = _audit(item.last_error_detail)
            if audit:
                _count_attempt(audit, models, result_counts, rejections)
                cache_hits += int(bool(audit.get("cache_hit")))
                attempts = audit.get("prior_attempts", [])
                if isinstance(attempts, list):
                    retries += len(attempts)
                    for entry in attempts:
                        if isinstance(entry, dict):
                            _count_attempt(entry, models, result_counts, rejections)

        final = [decisions.get(item.adopted_decision_id) for item in items]
        final = [decision for decision in final if decision is not None]
        classified = [decision for decision in final if decision.classification_status == "classified"]
        review = [decision for decision in final if decision.classification_status == "needs_review"]
        newly_classified = sum(
            decision.classification_run_id == run.id
            and (old := prior.get(decision.supersedes_decision_id)) is not None
            and old.classification_status == "needs_review"
            and decision.classification_status == "classified"
            for decision in final
        )
        changed_existing = sum(
            decision.classification_run_id == run.id
            and (old := prior.get(decision.supersedes_decision_id)) is not None
            and old.classification_status == "classified"
            and _classification_tuple(old) != _classification_tuple(decision)
            for decision in final
        )
        report: dict[str, object] = {
            "run_id": run.run_id,
            "run_status": run.status,
            "prompt_version": run.prompt_version,
            "target_total": run.target_count,
            "excluded": run.excluded_count,
            "stages": dict(sorted(stages.items())),
            "completed": stages["decided"],
            "technical_failed": stages["failed"],
            "classified": len(classified),
            "needs_review": len(review),
            "newly_classified_from_review": newly_classified,
            "changed_existing_classified": changed_existing,
            "model_calls": dict(sorted(models.items())),
            "model_results": dict(sorted(result_counts.items())),
            "rejection_codes": dict(sorted(rejections.items())),
            "retry_attempts": retries,
            "cache_hits": cache_hits,
            "reconciled": (
                len(items) == run.target_count
                and run.excluded_count == 0
                and stages["queued"] == 0
                and stages["content"] == 0
                and stages["decided"] + stages["failed"] == run.target_count
            ),
        }
        _write_json(output_root / "agnes_run_report.json", report)
        with (output_root / "remaining_needs_review.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=("oa_item_key", "decision_id", "title", "reason"))
            writer.writeheader()
            for decision in review:
                writer.writerow({
                    "oa_item_key": decision.oa_item_key,
                    "decision_id": decision.id,
                    "title": decision.normalized_title,
                    "reason": _reason(decision.classification_reason_json),
                })
    return report


def _count_attempt(
    audit: dict[str, object], models: Counter[str], results: Counter[str], rejections: Counter[str]
) -> None:
    provider = audit.get("provider")
    if isinstance(provider, str):
        models[provider] += 1
    result = audit.get("result")
    if isinstance(result, str):
        results[result] += 1
    rejection = audit.get("rejection_code")
    if isinstance(rejection, str):
        rejections[rejection] += 1


def _audit(raw: str | None) -> dict[str, object]:
    try:
        value: Any = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _reason(raw: str) -> str:
    value = _audit(raw)
    reason = value.get("reason")
    return reason if isinstance(reason, str) else ""


def _classification_tuple(decision: ClassificationDecision) -> tuple[object, ...]:
    return (
        decision.classification_status,
        decision.content_origin,
        decision.flow_type,
        decision.business_category,
        decision.canonical_issuer,
    )


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
