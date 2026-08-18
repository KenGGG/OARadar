"""持久化数据清理预检计划。"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.data_governance.inventory import inventory_candidates
from oa_knowledge.data_governance.models import DATA_RULES_VERSION
from oa_knowledge.db.models import CleanupItem, CleanupRun


@dataclass(frozen=True)
class CategorySummary:
    count: int
    bytes: int


@dataclass(frozen=True)
class CleanupPlanSummary:
    run_id: int
    status: str
    candidate_count: int
    candidate_bytes: int
    categories: dict[str, CategorySummary]


def build_cleanup_plan(
    settings: Settings,
    engine: Engine,
    *,
    categories: set[str],
) -> CleanupPlanSummary:
    """Build and persist a privacy-safe cleanup plan without moving files."""
    with Session(engine) as session:
        run = CleanupRun(
            status="planning",
            rules_version=DATA_RULES_VERSION,
            categories_json=json.dumps(sorted(categories), ensure_ascii=True),
        )
        session.add(run)
        session.flush()
        try:
            candidates = inventory_candidates(session, settings, categories)
            totals: dict[str, list[int]] = {
                category: [0, 0] for category in sorted(categories)
            }
            for candidate in candidates:
                session.add(CleanupItem(
                    cleanup_run_id=run.id,
                    relative_path=candidate.relative_path,
                    category=candidate.category,
                    size_bytes=candidate.size_bytes,
                    preflight_sha256=candidate.sha256,
                    status="planned",
                    reason_code=candidate.reason_code,
                ))
                totals[candidate.category][0] += 1
                totals[candidate.category][1] += candidate.size_bytes
            run.candidate_count = len(candidates)
            run.candidate_bytes = sum(candidate.size_bytes for candidate in candidates)
            run.status = "planned"
            session.commit()
        except Exception:
            session.rollback()
            raise
        return CleanupPlanSummary(
            run_id=run.id,
            status=run.status,
            candidate_count=run.candidate_count,
            candidate_bytes=run.candidate_bytes,
            categories={
                category: CategorySummary(count=values[0], bytes=values[1])
                for category, values in totals.items()
            },
        )
