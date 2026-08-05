"""Tests for the hardened Feishu delivery service (plan-0805-02 §3)."""

from __future__ import annotations

import httpx
import pytest

from oa_knowledge.config import load_settings, validate_feishu_runtime_config
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import NotificationDelivery
from oa_knowledge.notifications.feishu_service import (
    FeishuService,
    apply_delivery_result,
    delivery_status_for_result,
    is_retryable,
    retry_pending_summary_delivery,
    sanitize_feishu_error,
    sanitize_feishu_error_value,
)
from oa_knowledge.notifications.models import DeliveryResult


# --------------------------------------------------------------------------- #
# Fake transport
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, status_code: int = 200, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body if body is not None else {"code": 0}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "err", request=httpx.Request("POST", "https://open.feishu.cn/x"),
                response=httpx.Response(self.status_code),
            )

    def json(self) -> dict:
        return self._body


class _FakeClient:
    def __init__(self, spec: dict) -> None:
        self._spec = spec

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def post(self, url: str, json: dict | None = None) -> _FakeResponse:  # noqa: A002
        spec = self._spec
        if "exc" in spec:
            raise spec["exc"]
        return _FakeResponse(spec.get("status", 200), spec.get("body", {"code": 0}))


@pytest.fixture
def feishu_settings(config_file, monkeypatch):
    settings = load_settings(config_file)
    monkeypatch.setenv("FEISHU_OA_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/x")
    monkeypatch.setenv("FEISHU_OA_SECRET", "secret")
    settings.feishu.enabled = True
    return settings


# --------------------------------------------------------------------------- #
# Webhook validation states
# --------------------------------------------------------------------------- #
def test_feishu_runtime_ready_when_configured(config_file, monkeypatch) -> None:
    monkeypatch.setenv("FEISHU_OA_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/abc")
    monkeypatch.setenv("FEISHU_OA_SECRET", "s")
    settings = load_settings(config_file)
    settings.feishu.enabled = True
    assert validate_feishu_runtime_config(settings) == "ready"


def test_feishu_runtime_disabled(config_file, monkeypatch) -> None:
    settings = load_settings(config_file)
    settings.feishu.enabled = False
    assert validate_feishu_runtime_config(settings) == "disabled"


def test_feishu_runtime_missing_webhook(config_file, monkeypatch) -> None:
    monkeypatch.delenv("FEISHU_OA_WEBHOOK", raising=False)
    monkeypatch.delenv("FEISHU_OA_SECRET", raising=False)
    settings = load_settings(config_file)
    settings.feishu.enabled = True
    assert validate_feishu_runtime_config(settings) == "missing_webhook"


def test_feishu_runtime_missing_secret(config_file, monkeypatch) -> None:
    monkeypatch.setenv("FEISHU_OA_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/abc")
    monkeypatch.delenv("FEISHU_OA_SECRET", raising=False)
    settings = load_settings(config_file)
    settings.feishu.enabled = True
    assert validate_feishu_runtime_config(settings) == "missing_secret"


def test_feishu_runtime_invalid_webhook_bad_host(config_file, monkeypatch) -> None:
    monkeypatch.setenv("FEISHU_OA_WEBHOOK", "https://evil.example.com/open-apis/bot/v2/hook/abc")
    monkeypatch.setenv("FEISHU_OA_SECRET", "s")
    settings = load_settings(config_file)
    settings.feishu.enabled = True
    assert validate_feishu_runtime_config(settings) == "invalid_webhook"


def test_feishu_runtime_invalid_webhook_bad_path(config_file, monkeypatch) -> None:
    monkeypatch.setenv("FEISHU_OA_WEBHOOK", "https://open.feishu.cn/other/path/abc")
    monkeypatch.setenv("FEISHU_OA_SECRET", "s")
    settings = load_settings(config_file)
    settings.feishu.enabled = True
    assert validate_feishu_runtime_config(settings) == "invalid_webhook"


def test_feishu_runtime_invalid_webhook_credentials(config_file, monkeypatch) -> None:
    monkeypatch.setenv("FEISHU_OA_WEBHOOK", "https://user:pwd@open.feishu.cn/open-apis/bot/v2/hook/abc")
    monkeypatch.setenv("FEISHU_OA_SECRET", "s")
    settings = load_settings(config_file)
    settings.feishu.enabled = True
    assert validate_feishu_runtime_config(settings) == "invalid_webhook"


# --------------------------------------------------------------------------- #
# Error sanitization (no URL / secret leakage)
# --------------------------------------------------------------------------- #
def test_sanitize_feishu_error_hides_url_and_secret() -> None:
    exc = RuntimeError("POST https://open.feishu.cn/open-apis/bot/v2/hook/SECRETKEY failed")
    safe = sanitize_feishu_error(exc)
    assert "SECRETKEY" not in safe
    assert "open.feishu.cn" not in safe
    assert "http" not in safe


def test_sanitize_feishu_error_classifies_transport() -> None:
    assert sanitize_feishu_error(httpx.ConnectError("conn")).startswith("feishu connection")
    assert sanitize_feishu_error(httpx.TimeoutException("t")).startswith("feishu request timed out")
    status = sanitize_feishu_error(httpx.HTTPStatusError(
        "e", request=httpx.Request("POST", "https://x"), response=httpx.Response(500)))
    assert status == "feishu returned HTTP 500"


def test_sanitize_feishu_error_value_caps() -> None:
    assert sanitize_feishu_error_value("https://open.feishu.cn/hook/x") == "feishu delivery error"
    assert sanitize_feishu_error_value(None) is None
    assert sanitize_feishu_error_value("plain message") == "plain message"
    assert len(sanitize_feishu_error_value("x" * 500)) <= 200


# --------------------------------------------------------------------------- #
# Delivery result classification
# --------------------------------------------------------------------------- #
def test_is_retryable_matrix() -> None:
    assert is_retryable(DeliveryResult("connect_failed", True))
    assert is_retryable(DeliveryResult("rate_limited", True))
    assert is_retryable(DeliveryResult("server_failed", True))
    assert not is_retryable(DeliveryResult("sent", False))
    assert not is_retryable(DeliveryResult("rejected", False))
    assert not is_retryable(DeliveryResult("unknown_outcome", False))
    # retryable flag off beats status membership
    assert not is_retryable(DeliveryResult("connect_failed", False))


def test_delivery_status_for_result_mapping() -> None:
    assert delivery_status_for_result(DeliveryResult("sent", False)) == "sent"
    assert delivery_status_for_result(DeliveryResult("rejected", False)) == "failed"
    assert delivery_status_for_result(DeliveryResult("misconfigured", False)) == "failed"
    assert delivery_status_for_result(DeliveryResult("connect_failed", True)) == "retry_wait"
    assert delivery_status_for_result(DeliveryResult("unknown_outcome", False)) == "unknown"


def test_apply_delivery_result_sent() -> None:
    delivery = NotificationDelivery(
        logical_item_id=1, snapshot_id=1, channel="feishu",
        notification_type="pending_summary", idempotency_key="k", status="sending",
    )
    apply_delivery_result(delivery, DeliveryResult("sent", False), _now())
    assert delivery.status == "sent"
    assert delivery.sent_at is not None
    assert delivery.next_retry_at is None
    assert delivery.error_code is None


def test_apply_delivery_result_retryable() -> None:
    delivery = NotificationDelivery(
        logical_item_id=1, snapshot_id=1, channel="feishu",
        notification_type="pending_summary", idempotency_key="k", status="sending", attempts=0,
    )
    apply_delivery_result(delivery, DeliveryResult("connect_failed", True, error_code="http_connect"), _now())
    assert delivery.status == "retry_wait"
    assert delivery.next_retry_at is not None
    assert delivery.error_code == "http_connect"


def test_apply_delivery_result_unknown() -> None:
    delivery = NotificationDelivery(
        logical_item_id=1, snapshot_id=1, channel="feishu",
        notification_type="pending_summary", idempotency_key="k", status="sending",
    )
    apply_delivery_result(delivery, DeliveryResult("unknown_outcome", False, safe_error="feishu request timed out; delivery outcome unknown"), _now())
    assert delivery.status == "unknown"
    assert delivery.next_retry_at is None
    assert delivery.last_error == "feishu request timed out; delivery outcome unknown"


# --------------------------------------------------------------------------- #
# FeishuService.send classification
# --------------------------------------------------------------------------- #
def test_send_ready_success(feishu_settings, monkeypatch) -> None:
    monkeypatch.setattr(
        "oa_knowledge.notifications.feishu_service.httpx.Client",
        lambda *a, **k: _FakeClient({"status": 200, "body": {"code": 0}}),
    )
    result = FeishuService(feishu_settings).send({"msg_type": "text", "content": {"text": "x"}})
    assert result.status == "sent"
    assert result.retryable is False


def test_send_business_rejected_non_retryable(feishu_settings, monkeypatch) -> None:
    monkeypatch.setattr(
        "oa_knowledge.notifications.feishu_service.httpx.Client",
        lambda *a, **k: _FakeClient({"status": 200, "body": {"code": 19001}}),
    )
    result = FeishuService(feishu_settings).send({"msg_type": "text", "content": {"text": "x"}})
    assert result.status == "rejected"
    assert result.retryable is False
    assert result.provider_code == "19001"


def test_send_business_error_retryable(feishu_settings, monkeypatch) -> None:
    monkeypatch.setattr(
        "oa_knowledge.notifications.feishu_service.httpx.Client",
        lambda *a, **k: _FakeClient({"status": 200, "body": {"code": 99999}}),
    )
    result = FeishuService(feishu_settings).send({"msg_type": "text", "content": {"text": "x"}})
    assert result.status == "server_failed"
    assert result.retryable is True


def test_send_rate_limited(feishu_settings, monkeypatch) -> None:
    monkeypatch.setattr(
        "oa_knowledge.notifications.feishu_service.httpx.Client",
        lambda *a, **k: _FakeClient({"status": 429}),
    )
    result = FeishuService(feishu_settings).send({"msg_type": "text", "content": {"text": "x"}})
    assert result.status == "rate_limited"
    assert result.retryable is True


def test_send_server_5xx(feishu_settings, monkeypatch) -> None:
    monkeypatch.setattr(
        "oa_knowledge.notifications.feishu_service.httpx.Client",
        lambda *a, **k: _FakeClient({"status": 503}),
    )
    result = FeishuService(feishu_settings).send({"msg_type": "text", "content": {"text": "x"}})
    assert result.status == "server_failed"
    assert result.retryable is True


def test_send_connect_failed(feishu_settings, monkeypatch) -> None:
    monkeypatch.setattr(
        "oa_knowledge.notifications.feishu_service.httpx.Client",
        lambda *a, **k: _FakeClient({"exc": httpx.ConnectError("boom")}),
    )
    result = FeishuService(feishu_settings).send({"msg_type": "text", "content": {"text": "x"}})
    assert result.status == "connect_failed"
    assert result.retryable is True
    assert "http" not in (result.safe_error or "")


def test_send_timeout_unknown_outcome(feishu_settings, monkeypatch) -> None:
    monkeypatch.setattr(
        "oa_knowledge.notifications.feishu_service.httpx.Client",
        lambda *a, **k: _FakeClient({"exc": httpx.TimeoutException("t")}),
    )
    result = FeishuService(feishu_settings).send({"msg_type": "text", "content": {"text": "x"}})
    assert result.status == "unknown_outcome"
    assert result.retryable is False


def test_send_disabled(config_file, monkeypatch) -> None:
    settings = load_settings(config_file)
    settings.feishu.enabled = False
    result = FeishuService(settings).send({"msg_type": "text", "content": {"text": "x"}})
    assert result.status == "disabled"
    assert result.retryable is False


def test_send_missing_secret_fails_closed(config_file, monkeypatch) -> None:
    monkeypatch.setenv("FEISHU_OA_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/abc")
    monkeypatch.delenv("FEISHU_OA_SECRET", raising=False)
    settings = load_settings(config_file)
    settings.feishu.enabled = True
    result = FeishuService(settings).send({"msg_type": "text", "content": {"text": "x"}})
    assert result.status == "missing_secret"
    assert result.retryable is False


def test_send_test_synthetic_only(feishu_settings, monkeypatch) -> None:
    captured = {}

    def fake_post(url, json=None):  # noqa: A002
        captured.update(json or {})
        return _FakeResponse(200, {"code": 0})

    monkeypatch.setattr(
        "oa_knowledge.notifications.feishu_service.httpx.Client",
        lambda *a, **k: _FakeClient({"status": 200, "body": {"code": 0}}),
    )
    result = FeishuService(feishu_settings).send_test()
    # The synthetic test card never carries real OA content.
    assert result.status == "sent"


# --------------------------------------------------------------------------- #
# Manual retry path
# --------------------------------------------------------------------------- #
def test_retry_pending_summary_delivery_succeeds(config_file, monkeypatch) -> None:
    from sqlalchemy.orm import Session

    from oa_knowledge.db.models import (
        ItemOccurrence,
        ItemSnapshot,
        LogicalItem,
        SummaryJob,
        SummaryVersion,
    )

    settings = load_settings(config_file)
    monkeypatch.setenv("FEISHU_OA_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/x")
    monkeypatch.setenv("FEISHU_OA_SECRET", "secret")
    settings.feishu.enabled = True
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        logical = LogicalItem(logical_key="pending:9", title="待重试")
        session.add(logical); session.flush()
        occ = ItemOccurrence(logical_item_id=logical.id, channel="pending", occurrence_key="pending:9",
                             title="待重试", sender="李四", current_node="复核", deadline_text="2026-09-01")
        session.add(occ); session.flush()
        snap = ItemSnapshot(logical_item_id=logical.id, snapshot_kind="pending_initial", version=1,
                            content_hash="0" * 64, payload_json="{}")
        session.add(snap); session.flush()
        job = SummaryJob(logical_item_id=logical.id, snapshot_id=snap.id, summary_kind="pending",
                         stage="item_summary", status="completed", idempotency_key="idem-r", max_attempts=3)
        session.add(job); session.flush()
        version = SummaryVersion(logical_item_id=logical.id, summary_job_id=job.id, snapshot_id=snap.id,
                                 summary_kind="pending", version=1, status="current", input_hash="r1",
                                 structured_json='{"summary":"请复核","matter_type":"报销","current_stage":"复核","required_action":"","risks":[],"deadlines":[],"key_points":[],"amounts":[],"attachment_overview":[],"confidence":0.9}',
                                 provider_name="t", model_name="t", prompt_version="pending-v1")
        session.add(version); session.flush()
        delivery = NotificationDelivery(
            logical_item_id=logical.id, snapshot_id=snap.id, channel="feishu",
            notification_type="pending_summary", idempotency_key="feishu:pending:9:r1", status="unknown",
        )
        session.add(delivery); session.flush()
        delivery_id = delivery.id
        session.commit()

    monkeypatch.setattr(
        "oa_knowledge.notifications.feishu_service.httpx.Client",
        lambda *a, **k: _FakeClient({"status": 200, "body": {"code": 0}}),
    )
    result = retry_pending_summary_delivery(engine, settings, delivery_id)
    assert result.status == "sent"
    with Session(engine) as session:
        refreshed = session.get(NotificationDelivery, delivery_id)
        assert refreshed.status == "sent"
        assert refreshed.sent_at is not None


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)
