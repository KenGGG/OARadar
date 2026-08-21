"""Local, deterministic classification suggestions and confirmations."""

from datetime import datetime, timezone
import json

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import RebuildConfig
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import OAItem, RebuildClassificationEvent
from oa_knowledge.rebuild.classification import (
    bulk_confirm_suggested,
    confirm_classification,
    seed_classification_suggestions,
    suggest_classification,
)


@pytest.fixture
def session(tmp_path):
    database_path = tmp_path / "state" / "test.db"
    upgrade_database(database_path)
    engine = create_db_engine(database_path)
    with Session(engine) as session:
        yield session


@pytest.fixture
def done_item(session: Session) -> OAItem:
    item = OAItem(oa_item_key="done:classification", source_channel="done", title="普通事项")
    session.add(item)
    session.flush()
    return item


def test_internal_suggestion_uses_fixed_category(done_item: OAItem) -> None:
    done_item.title = "内部风险检查事项"

    result = suggest_classification(done_item, {})

    assert result.source_type == "internal"
    assert result.internal_category == "风险管理"
    assert result.external_issuer is None
    assert result.confidence >= 0.90
    assert result.state == "suggested"


def test_external_alias_is_normalized(done_item: OAItem) -> None:
    done_item.sender = "示例市工信局"

    result = suggest_classification(done_item, {"示例市工信局": "示例市工业和信息化局"})

    assert result.source_type == "external"
    assert result.external_issuer == "示例市工业和信息化局"
    assert result.internal_category is None
    assert result.confidence >= 0.90
    assert result.state == "suggested"


def test_unclear_item_requires_review(done_item: OAItem) -> None:
    done_item.title = "普通事项"
    done_item.sender = None

    assert suggest_classification(done_item, {}).state == "needs_review"


def test_external_company_name_without_alias_requires_review(done_item: OAItem) -> None:
    done_item.title = "财务风险检查事项"
    done_item.sender = "广州凯得金融服务集团有限公司"

    result = suggest_classification(done_item, {})

    assert result.state == "needs_review"
    assert result.confidence < 0.90
    assert result.internal_category is None


def test_multiple_internal_category_matches_require_review(done_item: OAItem) -> None:
    done_item.title = "内部财务风险检查事项"

    result = suggest_classification(done_item, {})

    assert result.source_type == "internal"
    assert result.internal_category is None
    assert result.state == "needs_review"
    assert result.confidence < 0.90


def test_external_alias_config_requires_nonempty_names() -> None:
    with pytest.raises(ValidationError):
        RebuildConfig(external_issuer_aliases={"示例简称": ""})


@pytest.mark.parametrize(
    ("source_type", "internal_category", "external_issuer"),
    [
        ("other", "风险管理", None),
        ("internal", "错误分类", None),
        ("external", None, "  "),
        ("internal", "风险管理", "示例市工业和信息化局"),
        ("external", "风险管理", "示例市工业和信息化局"),
    ],
)
def test_confirmation_rejects_invalid_classification_shape(
    session: Session,
    done_item: OAItem,
    source_type: str,
    internal_category: str | None,
    external_issuer: str | None,
) -> None:
    with pytest.raises(ValueError):
        confirm_classification(
            session, done_item.id, source_type=source_type,
            internal_category=internal_category, external_issuer=external_issuer,
            confirmed_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
        )


def test_confirmation_records_redacted_classification_audit_event(session: Session, done_item: OAItem) -> None:
    done_item.source_type = "internal"
    done_item.internal_category = "风险管理"
    done_item.classification_state = "suggested"
    done_item.classification_confidence = 0.95

    confirmed = confirm_classification(
        session, done_item.id, source_type="external", internal_category=None,
        external_issuer="示例市工业和信息化局",
        confirmed_at=datetime(2026, 8, 21, tzinfo=timezone.utc), actor="reviewer",
    )

    event = session.scalar(select(RebuildClassificationEvent))
    assert confirmed.classification_state == "confirmed"
    assert confirmed.classification_source == "manual"
    assert event is not None
    assert event.actor == "reviewer"
    previous = json.loads(event.previous_classification_json)
    current = json.loads(event.current_classification_json)
    assert previous["internal_category"] == "风险管理"
    assert current == {
        "source_type": "external", "internal_category": None,
        "external_issuer": "示例市工业和信息化局", "classification_state": "confirmed",
        "classification_confidence": 1.0,
        "classification_confirmed_at": "2026-08-21T00:00:00+00:00",
        "classification_source": "manual",
    }
    assert "title" not in event.previous_classification_json


def test_bulk_confirmation_skips_needs_review_and_malformed_rows(session: Session) -> None:
    valid = OAItem(
        oa_item_key="done:valid", source_channel="done", title="内部风险检查事项",
        source_type="internal", internal_category="风险管理", classification_state="suggested",
        classification_confidence=0.95,
    )
    needs_review = OAItem(
        oa_item_key="done:review", source_channel="done", title="内部风险检查事项",
        source_type="internal", internal_category="风险管理", classification_state="needs_review",
        classification_confidence=0.95,
    )
    malformed = OAItem(
        oa_item_key="done:malformed", source_channel="done", title="内部风险检查事项",
        source_type="internal", internal_category="not-a-category", classification_state="suggested",
        classification_confidence=0.95,
    )
    session.add_all((valid, needs_review, malformed))
    session.flush()

    count = bulk_confirm_suggested(
        session, "internal", confirmed_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )

    assert count == 1
    assert valid.classification_state == "confirmed"
    assert needs_review.classification_state == "needs_review"
    assert malformed.classification_state == "suggested"
    assert session.scalars(select(RebuildClassificationEvent)).all()[0].oa_item_id == valid.id


def test_seed_suggestions_only_updates_unconfirmed_done_rows_idempotently(session: Session) -> None:
    suggested = OAItem(oa_item_key="done:suggested", source_channel="done", title="内部风险检查事项")
    unclear = OAItem(oa_item_key="done:unclear", source_channel="done", title="普通事项")
    confirmed = OAItem(
        oa_item_key="done:confirmed", source_channel="done", title="普通事项",
        source_type="external", external_issuer="已确认机构", classification_state="confirmed",
        classification_confirmed_at=datetime(2026, 8, 20, tzinfo=timezone.utc), classification_source="manual",
    )
    pending = OAItem(oa_item_key="pending:ignored", source_channel="pending", title="内部风险检查事项")
    session.add_all((suggested, unclear, confirmed, pending))
    session.flush()

    first = seed_classification_suggestions(session, {})
    second = seed_classification_suggestions(session, {})

    assert first == {"suggested": 1, "needs_review": 1}
    assert second == first
    assert suggested.classification_state == "suggested"
    assert suggested.classification_confirmed_at is None
    assert unclear.classification_state == "needs_review"
    assert confirmed.external_issuer == "已确认机构"
    assert pending.classification_state == "needs_review"
    assert session.scalars(select(RebuildClassificationEvent)).all() == []
