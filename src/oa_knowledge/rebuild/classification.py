"""Deterministic, human-gated classification for existing Done items."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.db.models import OAItem, RebuildClassificationEvent


INTERNAL_CATEGORIES = (
    "公司治理", "经营管理", "业务项目", "风险管理",
    "财务资金", "人力行政", "信息化", "其他内部",
)
SUGGESTION_THRESHOLD = 0.90
_CATEGORY_RULES = (
    ("风险管理", ("风险", "合规", "内控", "审计", "授信", "租后")),
    ("财务资金", ("财务", "预算", "资金", "报销", "会计", "税")),
    ("人力行政", ("人力", "招聘", "绩效", "行政", "党群", "工会")),
    ("信息化", ("信息化", "系统", "数据", "网络", "安全")),
    ("业务项目", ("项目", "客户", "业务", "投资", "融资", "合同")),
    ("公司治理", ("董事会", "股东", "治理", "章程", "决议")),
    ("经营管理", ("经营", "管理", "会议", "计划", "通知")),
)
_INTERNAL_MARKERS = ("内部", "本公司", "公司", "部门")


@dataclass(frozen=True)
class ClassificationDecision:
    source_type: str
    internal_category: str | None
    external_issuer: str | None
    confidence: float
    state: str


def _normalized_text(value: str | None) -> str | None:
    if not value:
        return None
    normalized = " ".join(value.split()).strip("：:;；，,。")
    return normalized or None


def _suggestion_state(confidence: float) -> str:
    return "suggested" if confidence >= SUGGESTION_THRESHOLD else "needs_review"


def suggest_classification(item: OAItem, issuer_aliases: dict[str, str]) -> ClassificationDecision:
    """Return a conservative local-only suggestion without changing ``item``."""
    title = item.title or ""
    sender = _normalized_text(item.sender)
    aliases = {
        normalized_alias: normalized_target
        for alias, target in issuer_aliases.items()
        if (normalized_alias := _normalized_text(alias))
        and (normalized_target := _normalized_text(target))
    }
    if sender and sender in aliases:
        confidence = 0.95
        return ClassificationDecision(
            source_type="external", internal_category=None, external_issuer=aliases[sender],
            confidence=confidence, state=_suggestion_state(confidence),
        )

    combined = f"{title} {sender or ''}"
    if any(marker in combined for marker in _INTERNAL_MARKERS):
        category = next(
            (name for name, keywords in _CATEGORY_RULES if any(keyword in combined for keyword in keywords)),
            "其他内部",
        )
        confidence = 0.95
        return ClassificationDecision(
            source_type="internal", internal_category=category, external_issuer=None,
            confidence=confidence, state=_suggestion_state(confidence),
        )

    return ClassificationDecision(
        source_type="unknown", internal_category=None, external_issuer=None,
        confidence=0.0, state="needs_review",
    )


def _classification_values(item: OAItem) -> dict[str, object]:
    confirmed_at = item.classification_confirmed_at
    return {
        "source_type": item.source_type,
        "internal_category": item.internal_category,
        "external_issuer": item.external_issuer,
        "classification_state": item.classification_state,
        "classification_confidence": item.classification_confidence,
        "classification_confirmed_at": confirmed_at.isoformat() if confirmed_at else None,
        "classification_source": item.classification_source,
    }


def _validate_shape(
    source_type: str,
    internal_category: str | None,
    external_issuer: str | None,
) -> tuple[str | None, str | None]:
    if source_type not in {"internal", "external"}:
        raise ValueError("source_type must be internal or external")
    category = _normalized_text(internal_category)
    issuer = _normalized_text(external_issuer)
    if source_type == "internal":
        if category not in INTERNAL_CATEGORIES or issuer is not None:
            raise ValueError("internal classification requires one fixed category and no external issuer")
        return category, None
    if category is not None or issuer is None:
        raise ValueError("external classification requires a non-empty issuer and no internal category")
    return None, issuer


def confirm_classification(
    session: Session,
    item_id: int,
    *,
    source_type: str,
    internal_category: str | None,
    external_issuer: str | None,
    confirmed_at: datetime,
    actor: str = "local_web",
) -> OAItem:
    """Persist one reviewed classification and a metadata-only local audit event."""
    category, issuer = _validate_shape(source_type, internal_category, external_issuer)
    item = session.get(OAItem, item_id)
    if item is None:
        raise LookupError("OA item not found")
    previous = _classification_values(item)
    item.source_type = source_type
    item.internal_category = category
    item.external_issuer = issuer
    item.classification_state = "confirmed"
    item.classification_confidence = 1.0
    item.classification_confirmed_at = confirmed_at
    item.classification_source = "manual"
    current = _classification_values(item)
    session.add(RebuildClassificationEvent(
        oa_item_id=item.id,
        previous_classification_json=json.dumps(previous, ensure_ascii=False, sort_keys=True),
        current_classification_json=json.dumps(current, ensure_ascii=False, sort_keys=True),
        actor=actor,
    ))
    session.flush()
    return item


def _is_structurally_valid(item: OAItem) -> bool:
    try:
        _validate_shape(item.source_type or "", item.internal_category, item.external_issuer)
    except ValueError:
        return False
    return True


def bulk_confirm_suggested(
    session: Session,
    source_type: Literal["internal", "external"],
    confirmed_at: datetime,
) -> int:
    """Confirm only high-confidence, structurally valid suggestions of one kind."""
    if source_type not in {"internal", "external"}:
        raise ValueError("source_type must be internal or external")
    items = session.scalars(select(OAItem).where(
        OAItem.source_channel == "done",
        OAItem.classification_state == "suggested",
        OAItem.classification_confidence >= SUGGESTION_THRESHOLD,
        OAItem.source_type == source_type,
    ).order_by(OAItem.id)).all()
    confirmed = 0
    for item in items:
        if not _is_structurally_valid(item):
            continue
        confirm_classification(
            session, item.id, source_type=source_type,
            internal_category=item.internal_category, external_issuer=item.external_issuer,
            confirmed_at=confirmed_at,
        )
        confirmed += 1
    return confirmed


def seed_classification_suggestions(session: Session, issuer_aliases: dict[str, str]) -> dict[str, int]:
    """Seed deterministic suggestions for unconfirmed Done rows without confirming them."""
    counts = {"suggested": 0, "needs_review": 0}
    items = session.scalars(select(OAItem).where(
        OAItem.source_channel == "done",
        OAItem.classification_state != "confirmed",
    ).order_by(OAItem.id)).all()
    for item in items:
        decision = suggest_classification(item, issuer_aliases)
        item.source_type = decision.source_type
        item.internal_category = decision.internal_category
        item.external_issuer = decision.external_issuer
        item.classification_confidence = decision.confidence
        item.classification_state = decision.state
        item.classification_confirmed_at = None
        item.classification_source = "rule"
        counts[decision.state] += 1
    session.flush()
    return counts
