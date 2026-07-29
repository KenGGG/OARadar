"""Task extraction from classified OA items."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.models import OAItem, Task
from oa_knowledge.enrich.extractor import ExtractedTask, extract_task_candidates
from oa_knowledge.enrich.rules import classify_item

logger = logging.getLogger(__name__)


class TaskExtractor:
    """Extracts structured tasks from OA items."""

    def __init__(self, settings: Settings, engine=None) -> None:
        self.settings = settings
        self._engine = engine

    @property
    def engine(self):
        if self._engine is None:
            from oa_knowledge.db.engine import create_db_engine
            self._engine = create_db_engine(self.settings.database_path)
        return self._engine

    def extract_from_item(self, item_id: int) -> list[ExtractedTask]:
        """Extract tasks from a single OA item by ID."""
        with Session(self.engine) as session:
            item = session.get(OAItem, item_id)
            if item is None:
                return []

            # 1. Try rule-based extraction from title
            tasks = extract_task_candidates("", title=item.title)

            # 2. Also check archive manifest for content hints
            if item.archive_relpath:
                archive_path = self.settings.data_root / item.archive_relpath
                manifest_path = archive_path / "manifest.json"
                if manifest_path.is_file():
                    try:
                        manifest = __import__("json").loads(manifest_path.read_text(encoding="utf-8"))
                        body_text = manifest.get("body_text", "") or ""
                        tasks.extend(extract_task_candidates(body_text, title=item.title))
                    except Exception:
                        pass

            # 3. Store in DB
            for task_data in tasks:
                task = Task(
                    source_item_id=item_id,
                    title=task_data.title,
                    action=task_data.action,
                    responsible_party=task_data.responsible_party,
                    deadline=task_data.deadline,
                    deadline_type=task_data.deadline_type,
                    source_kind=task_data.source_kind,
                    confidence=task_data.confidence,
                    evidence_text=task_data.evidence_text,
                    evidence_page=task_data.evidence_page,
                    needs_confirmation=task_data.needs_confirmation,
                    status="candidate",
                )
                session.add(task)
            session.commit()

            return tasks

    def extract_all_pending(self, limit: int = 50) -> dict:
        """Extract tasks from all items that have not yet had tasks extracted."""
        summary = {"processed": 0, "tasks_found": 0, "errors": []}

        with Session(self.engine) as session:
            items = (
                session.execute(
                    select(OAItem).where(
                        OAItem.pipeline_status.in_(("classified", "parsed"))
                    )
                    .order_by(OAItem.id)
                    .limit(limit)
                )
                .scalars()
                .all()
            )

        for item in items:
            try:
                tasks = self.extract_from_item(item.id)
                summary["tasks_found"] += len(tasks)
            except Exception as exc:
                summary["errors"].append(f"item={item.id}: {exc}")
            summary["processed"] += 1

        return summary
