from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import (
    ArchivedFile,
    OAItem,
    PipelineRun,
    RebuildClassificationEvent,
)
from oa_knowledge.rebuild.validation import ValidationCheck
from oa_knowledge.web import create_web_app, rebuild_views


@pytest.fixture
def client(config_file: Path) -> TestClient:
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True)
    upgrade_database(settings.database_path)
    return TestClient(create_web_app(settings))


@pytest.fixture
def seeded_items(client: TestClient) -> dict[str, int]:
    engine = create_db_engine(client.app.state.settings.database_path)
    try:
        with Session(engine) as session:
            internal = OAItem(
                oa_item_key="done:internal", source_channel="done", title="内部风险检查事项",
                document_number="synthetic-001", document_date=date(2026, 8, 20),
                source_type="internal", internal_category="风险管理", classification_state="suggested",
                classification_confidence=0.95, classification_source="rule",
            )
            review = OAItem(
                oa_item_key="done:review", source_channel="done", title="Synthetic review item",
                sender="Synthetic sender", classification_state="needs_review",
            )
            confirmed = OAItem(
                oa_item_key="done:confirmed", source_channel="done", title="Synthetic confirmed item",
                source_type="external", external_issuer="Synthetic institution", classification_state="confirmed",
                classification_confidence=1.0, classification_source="manual",
            )
            session.add_all((internal, review, confirmed))
            session.flush()
            session.add(ArchivedFile(
                oa_item_id=review.id, original_name="synthetic.pdf", attachment_key="synthetic-file",
                file_role="direct_attachment", source_container_key="synthetic-container",
            ))
            session.commit()
            return {"internal": internal.id, "review": review.id, "confirmed": confirmed.id}
    finally:
        engine.dispose()


def _csrf_headers(client: TestClient) -> dict[str, str]:
    client.get("/api/status")
    return {"x-csrf-token": client.cookies["oa_csrf"]}


def test_needs_review_page_redacts_body(client: TestClient, seeded_items: dict[str, int]) -> None:
    payload = client.get("/api/rebuild/classifications?group=needs_review").json()

    assert set(payload["items"][0]) == {
        "id", "title", "document_number", "sender", "item_date",
        "source_type", "internal_category", "external_issuer",
        "classification_state", "has_document_number", "attachment_count",
    }
    assert "body" not in payload["items"][0]
    assert payload["items"][0]["id"] == seeded_items["review"]
    assert payload["items"][0]["attachment_count"] == 1
    assert payload["total"] == 1


def test_classification_groups_paginate_done_items(client: TestClient, seeded_items: dict[str, int]) -> None:
    payload = client.get("/api/rebuild/classifications?group=internal&page=1&page_size=1").json()

    assert payload["page"] == 1
    assert payload["page_size"] == 1
    assert payload["total"] == 1
    assert payload["items"][0]["id"] == seeded_items["internal"]
    assert client.get("/api/rebuild/classifications?group=external").json()["items"][0]["id"] == seeded_items["confirmed"]


def test_confirm_requires_valid_transition_and_shape(client: TestClient, seeded_items: dict[str, int]) -> None:
    headers = _csrf_headers(client)

    invalid_shape = client.post(
        f"/api/rebuild/classifications/{seeded_items['review']}/confirm",
        json={"source_type": "internal", "internal_category": "invalid", "external_issuer": None}, headers=headers,
    )
    confirmed = client.post(
        f"/api/rebuild/classifications/{seeded_items['review']}/confirm",
        json={"source_type": "external", "internal_category": None, "external_issuer": "Synthetic issuer"}, headers=headers,
    )
    invalid_transition = client.post(
        f"/api/rebuild/classifications/{seeded_items['review']}/confirm",
        json={"source_type": "external", "internal_category": None, "external_issuer": "Synthetic issuer"}, headers=headers,
    )

    assert invalid_shape.status_code == 422
    assert confirmed.status_code == 200
    assert confirmed.json()["classification_state"] == "confirmed"
    assert confirmed.json()["attachment_count"] == 1
    assert invalid_transition.status_code == 409


def test_confirm_unknown_item_is_json_404(client: TestClient) -> None:
    response = client.post(
        "/api/rebuild/classifications/99999/confirm",
        json={"source_type": "external", "internal_category": None, "external_issuer": "Synthetic issuer"},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_bulk_confirm_does_not_confirm_needs_review(client: TestClient, seeded_items: dict[str, int]) -> None:
    response = client.post(
        "/api/rebuild/classifications/bulk-confirm", json={"source_type": "internal"}, headers=_csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.json()["confirmed"] == 1
    assert response.json()["needs_review_unchanged"] > 0


def test_bulk_confirm_only_changes_eligible_done_rows_once(client: TestClient) -> None:
    engine = create_db_engine(client.app.state.settings.database_path)
    try:
        with Session(engine) as session:
            rows = [
                OAItem(
                    oa_item_key="done:eligible", source_channel="done", title="Synthetic eligible",
                    source_type="internal", internal_category="风险管理", classification_state="suggested",
                    classification_confidence=0.95, classification_source="rule",
                ),
                OAItem(
                    oa_item_key="pending:eligible-shape", source_channel="pending", title="Synthetic pending",
                    source_type="internal", internal_category="风险管理", classification_state="suggested",
                    classification_confidence=0.95, classification_source="rule",
                ),
                OAItem(
                    oa_item_key="done:low-confidence", source_channel="done", title="Synthetic low confidence",
                    source_type="internal", internal_category="风险管理", classification_state="suggested",
                    classification_confidence=0.89, classification_source="rule",
                ),
                OAItem(
                    oa_item_key="done:wrong-source", source_channel="done", title="Synthetic external",
                    source_type="external", external_issuer="Synthetic institution", classification_state="suggested",
                    classification_confidence=0.95, classification_source="rule",
                ),
                OAItem(
                    oa_item_key="done:already-confirmed", source_channel="done", title="Synthetic confirmed",
                    source_type="internal", internal_category="风险管理", classification_state="confirmed",
                    classification_confidence=1.0, classification_source="manual",
                    classification_confirmed_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
                ),
            ]
            session.add_all(rows)
            session.commit()
            ids = {row.oa_item_key: row.id for row in rows}
    finally:
        engine.dispose()

    headers = _csrf_headers(client)
    first = client.post(
        "/api/rebuild/classifications/bulk-confirm", json={"source_type": "internal"}, headers=headers,
    )
    second = client.post(
        "/api/rebuild/classifications/bulk-confirm", json={"source_type": "internal"}, headers=headers,
    )

    assert first.json()["confirmed"] == 1
    assert second.json()["confirmed"] == 0
    engine = create_db_engine(client.app.state.settings.database_path)
    try:
        with Session(engine) as session:
            states = {
                item.oa_item_key: item.classification_state
                for item in session.scalars(select(OAItem).where(OAItem.id.in_(ids.values())))
            }
            events = session.scalars(select(RebuildClassificationEvent).where(
                RebuildClassificationEvent.oa_item_id.in_(ids.values()),
            )).all()
    finally:
        engine.dispose()
    assert states == {
        "done:eligible": "confirmed",
        "pending:eligible-shape": "suggested",
        "done:low-confidence": "suggested",
        "done:wrong-source": "suggested",
        "done:already-confirmed": "confirmed",
    }
    assert [event.oa_item_id for event in events] == [ids["done:eligible"]]


def test_concurrent_bulk_confirm_is_idempotent_with_one_audit_per_item(client: TestClient) -> None:
    item_count = 40
    engine = create_db_engine(client.app.state.settings.database_path)
    try:
        with Session(engine) as session:
            rows = [
                OAItem(
                    oa_item_key=f"done:bulk-concurrent-{index}", source_channel="done",
                    title=f"Synthetic concurrent {index}", source_type="internal",
                    internal_category="风险管理", classification_state="suggested",
                    classification_confidence=0.95, classification_source="rule",
                )
                for index in range(item_count)
            ]
            session.add_all(rows)
            session.commit()
            item_ids = [row.id for row in rows]
    finally:
        engine.dispose()

    start = threading.Barrier(2)

    def confirm_bulk() -> int:
        start.wait(timeout=5)
        return rebuild_views.bulk_confirm_rebuild_classifications(
            client.app.state.settings, source_type="internal",
        )["confirmed"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        counts = list(executor.map(lambda _: confirm_bulk(), range(2)))

    engine = create_db_engine(client.app.state.settings.database_path)
    try:
        with Session(engine) as session:
            events = session.scalars(select(RebuildClassificationEvent).where(
                RebuildClassificationEvent.oa_item_id.in_(item_ids),
            )).all()
    finally:
        engine.dispose()

    assert sorted(counts) == [0, item_count]
    assert len(events) == item_count
    assert {event.oa_item_id for event in events} == set(item_ids)


def test_suggest_is_explicit_and_summary_is_metadata_only(client: TestClient) -> None:
    engine = create_db_engine(client.app.state.settings.database_path)
    try:
        with Session(engine) as session:
            session.add(OAItem(
                oa_item_key="done:unseeded", source_channel="done", title="内部风险检查事项",
            ))
            session.commit()
    finally:
        engine.dispose()

    assert client.get("/api/rebuild/classifications?group=internal").json()["total"] == 0
    response = client.post("/api/rebuild/classifications/suggest", headers=_csrf_headers(client))
    summary = client.get("/api/rebuild/classification-summary")

    assert response.status_code == 200
    assert response.json() == {"suggested": 1, "needs_review": 0}
    assert summary.status_code == 200
    assert summary.json()["internal"]["suggested"] == 1


def test_markdown_rebuild_controls_expose_phase4_non_execution_gate(client: TestClient) -> None:
    """Removing the CAS gate must never let the production API create work."""
    headers = _csrf_headers(client)
    status = client.get("/api/rebuild/status")
    start = client.post("/api/rebuild/start", headers=headers)
    pause = client.post("/api/rebuild/pause", headers=headers)
    resume = client.post("/api/rebuild/resume", headers=headers)

    assert status.status_code == 200
    assert status.json()["execution_allowed"] is False
    for response in (start, pause, resume):
        assert response.status_code == 409
        assert response.json() == {
            "detail": "MARKDOWN_REBUILD_PHASE4_CAS_REQUIRED",
        }


def test_rebuild_validation_endpoint_exposes_aggregate_checks_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A validation report must never leak titles, paths, or source text."""
    engine = create_db_engine(client.app.state.settings.database_path)
    try:
        with Session(engine) as session:
            run = PipelineRun(
                run_key="synthetic-validation-web", pipeline_type="data_rebuild",
                status="completed", total_tasks=1, completed_tasks=1,
            )
            session.add(run)
            session.commit()
            run_id = run.id
    finally:
        engine.dispose()
    monkeypatch.setattr(
        rebuild_views,
        "validate_rebuild",
        lambda _session, _settings, observed_run_id: [
            ValidationCheck("SYNTHETIC_CHECK", observed_run_id == run_id, 3, 3),
        ],
    )
    monkeypatch.setattr(rebuild_views, "validation_passed", lambda _checks: True)

    response = client.get("/api/rebuild/validation")

    assert response.status_code == 200
    assert response.json() == {
        "available": True,
        "run_id": run_id,
        "passed": True,
        "checks": [{"code": "SYNTHETIC_CHECK", "ok": True, "expected": 3, "actual": 3}],
    }


def test_rebuild_validation_endpoint_is_empty_when_no_run_exists(client: TestClient) -> None:
    """The no-run response is a safe aggregate, not an exceptional detail leak."""
    response = client.get("/api/rebuild/validation")

    assert response.status_code == 200
    assert response.json() == {
        "available": False,
        "run_id": None,
        "passed": False,
        "checks": [],
    }


def test_concurrent_confirmation_allows_one_transition_and_one_audit_event(
    client: TestClient, seeded_items: dict[str, int], monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_get = rebuild_views.Session.get
    barrier = threading.Barrier(2)
    barrier_lock = threading.Lock()
    barrier_calls = 0

    def synchronized_get(self, entity, ident, *args, **kwargs):
        nonlocal barrier_calls
        item = original_get(self, entity, ident, *args, **kwargs)
        with barrier_lock:
            should_wait = entity is OAItem and ident == seeded_items["review"] and barrier_calls < 2
            barrier_calls += should_wait
        if should_wait:
            barrier.wait(timeout=5)
        return item

    monkeypatch.setattr(rebuild_views.Session, "get", synchronized_get)

    def confirm(issuer: str) -> str:
        try:
            rebuild_views.confirm_rebuild_classification(
                client.app.state.settings, seeded_items["review"], source_type="external",
                internal_category=None, external_issuer=issuer,
            )
        except RuntimeError:
            return "conflict"
        return "confirmed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(confirm, ("Synthetic issuer A", "Synthetic issuer B")))

    engine = create_db_engine(client.app.state.settings.database_path)
    try:
        with Session(engine) as session:
            events = session.scalars(select(RebuildClassificationEvent).where(
                RebuildClassificationEvent.oa_item_id == seeded_items["review"],
            )).all()
    finally:
        engine.dispose()

    assert sorted(outcomes) == ["confirmed", "conflict"]
    assert len(events) == 1
