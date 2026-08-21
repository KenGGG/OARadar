"""Narrow, metadata-only APIs for local rebuild-classification review."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import ArchivedFile, OAItem, PipelineRun
from oa_knowledge.rebuild.campaign import rebuild_status_summary
from oa_knowledge.rebuild.classification import (
    bulk_confirm_suggested,
    confirm_classification,
    seed_classification_suggestions,
)
from oa_knowledge.rebuild.validation import validate_rebuild, validation_passed

ClassificationGroup = Literal["internal", "external", "needs_review"]


def _item_payload(item: OAItem, attachment_count: int) -> dict:
    """Return review metadata only; OA body content is deliberately absent."""
    return {
        "id": item.id,
        "title": item.title,
        "document_number": item.document_number,
        "sender": item.sender,
        "item_date": item.document_date.isoformat() if item.document_date else None,
        "source_type": item.source_type,
        "internal_category": item.internal_category,
        "external_issuer": item.external_issuer,
        "classification_state": item.classification_state,
        "has_document_number": bool(item.document_number),
        "attachment_count": attachment_count,
    }


def _group_condition(group: ClassificationGroup):
    if group == "needs_review":
        return (OAItem.classification_state == "needs_review",)
    return (
        OAItem.source_type == group,
        OAItem.classification_state.in_(("suggested", "confirmed")),
    )


def classification_list(
    settings: Settings, *, group: ClassificationGroup, page: int, page_size: int,
) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            conditions = (OAItem.source_channel == "done", *_group_condition(group))
            base = select(OAItem).where(*conditions)
            total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
            items = session.scalars(
                base.order_by(OAItem.document_date.desc(), OAItem.id.desc())
                .offset((page - 1) * page_size).limit(page_size)
            ).all()
            counts = dict(session.execute(
                select(ArchivedFile.oa_item_id, func.count(ArchivedFile.id))
                .where(ArchivedFile.oa_item_id.in_([item.id for item in items]))
                .group_by(ArchivedFile.oa_item_id)
            ).all()) if items else {}
            return {
                "items": [_item_payload(item, counts.get(item.id, 0)) for item in items],
                "total": total,
                "page": page,
                "page_size": page_size,
            }
    finally:
        engine.dispose()


def classification_summary(settings: Settings) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            rows = session.execute(
                select(OAItem.source_type, OAItem.classification_state, func.count())
                .where(OAItem.source_channel == "done")
                .group_by(OAItem.source_type, OAItem.classification_state)
            ).all()
            summary = {
                "internal": {"suggested": 0, "confirmed": 0, "total": 0},
                "external": {"suggested": 0, "confirmed": 0, "total": 0},
                "needs_review": {"total": 0},
            }
            for source_type, state, count in rows:
                if state == "needs_review":
                    summary["needs_review"]["total"] += count
                elif source_type in {"internal", "external"}:
                    summary[source_type][state] = count
                    summary[source_type]["total"] += count
            return summary
    finally:
        engine.dispose()


def confirm_rebuild_classification(
    settings: Settings,
    item_id: int,
    *,
    source_type: str,
    internal_category: str | None,
    external_issuer: str | None,
) -> dict:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            item = session.get(OAItem, item_id)
            if item is None:
                raise LookupError("OA item not found")
            if item.source_channel != "done" or item.classification_state == "confirmed":
                raise RuntimeError("classification is not eligible for confirmation")
            claimed = session.execute(
                update(OAItem).where(
                    OAItem.id == item_id,
                    OAItem.source_channel == "done",
                    OAItem.classification_state != "confirmed",
                ).values(classification_state=item.classification_state)
            )
            if claimed.rowcount != 1:
                raise RuntimeError("classification is not eligible for confirmation")
            session.refresh(item)
            confirmed = confirm_classification(
                session, item_id, source_type=source_type, internal_category=internal_category,
                external_issuer=external_issuer, confirmed_at=datetime.now(UTC),
            )
            attachment_count = session.scalar(select(func.count(ArchivedFile.id)).where(
                ArchivedFile.oa_item_id == item_id,
            )) or 0
            session.commit()
            return _item_payload(confirmed, attachment_count)
    finally:
        engine.dispose()


def bulk_confirm_rebuild_classifications(settings: Settings, *, source_type: str) -> dict:
    if source_type not in {"internal", "external"}:
        raise ValueError("source_type must be internal or external")
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            # Acquire SQLite's single writer slot before reading candidates.
            # Concurrent bulk requests then serialize: the second request sees
            # the first request's committed confirmations and emits no audits.
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            needs_review_unchanged = session.scalar(select(func.count()).select_from(OAItem).where(
                OAItem.source_channel == "done", OAItem.classification_state == "needs_review",
            )) or 0
            confirmed = bulk_confirm_suggested(
                session, source_type, confirmed_at=datetime.now(UTC),
            )
            session.commit()
            return {"confirmed": confirmed, "needs_review_unchanged": needs_review_unchanged}
    finally:
        engine.dispose()


def seed_rebuild_classification_suggestions(settings: Settings) -> dict[str, int]:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            result = seed_classification_suggestions(session, settings.rebuild.external_issuer_aliases)
            session.commit()
            return result
    finally:
        engine.dispose()


def markdown_rebuild_status(settings: Settings) -> dict[str, object]:
    """Expose local progress metadata without paths, OA content, or error text."""
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            return rebuild_status_summary(session)
    finally:
        engine.dispose()


def rebuild_validation_summary(settings: Settings) -> dict[str, object]:
    """Return the newest rebuild's aggregate acceptance checks only.

    This deliberately presents stable check codes and counts, never the local
    file paths, OA metadata, parser messages, or report evidence examined by
    the validator.  A validation exception is also represented as one stable
    failed aggregate check instead of leaking exception text through the Web
    console.
    """
    engine = create_db_engine(settings.database_path, read_only=True)
    try:
        with Session(engine) as session:
            run = session.scalar(
                select(PipelineRun)
                .where(PipelineRun.pipeline_type == "data_rebuild")
                .order_by(PipelineRun.id.desc())
                .limit(1)
            )
            if run is None:
                return {
                    "available": False,
                    "run_id": None,
                    "passed": False,
                    "checks": [],
                }
            try:
                checks = validate_rebuild(session, settings, run.id)
            except Exception:  # noqa: BLE001 - acceptance errors remain redacted.
                return {
                    "available": True,
                    "run_id": run.id,
                    "passed": False,
                    "checks": [{
                        "code": "VALIDATION_UNAVAILABLE",
                        "ok": False,
                        "expected": 1,
                        "actual": 0,
                    }],
                }
            return {
                "available": True,
                "run_id": run.id,
                "passed": validation_passed(checks),
                "checks": [
                    {
                        "code": check.code,
                        "ok": check.ok,
                        "expected": check.expected,
                        "actual": check.actual,
                    }
                    for check in checks
                ],
            }
    finally:
        engine.dispose()
