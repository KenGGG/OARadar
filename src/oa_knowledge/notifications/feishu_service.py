"""Feishu delivery service with structured outcomes and privacy controls.

This is the service layer that the worker's notify stage delegates to
(plan-0805-02 §3). It never lets webhook URLs, signatures, or secrets reach
logs or error messages, and it classifies every outcome so the caller can
decide whether an automatic retry is safe.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from oa_knowledge.config import Settings, validate_feishu_runtime_config
from oa_knowledge.digest.feishu import FeishuNotifier
from oa_knowledge.notifications.models import RETRYABLE_STATUSES, DeliveryResult

logger = logging.getLogger(__name__)

# Feishu business codes that mean a permanent configuration/parameter problem:
# retrying will not help (plan-0805-02 §3.2). Other non-zero codes are treated
# as retryable server-side rejections.
NON_RETRYABLE_BUSINESS_CODES = frozenset({
    10001,  # invalid request parameters
    10005,  # unauthorized (bad webhook token)
    19001,  # sign match fail / invalid secret
    19021,  # invalid webhook / bot not found
    20003,  # insufficient permissions
})


def sanitize_feishu_error(exc: BaseException) -> str:
    """Return a safe error description that leaks no URL, host, or secret.

    httpx errors embed the full request URL (which *is* the credential for a
    custom bot). We collapse every such error to a code-level description.
    """
    if isinstance(exc, httpx.ConnectError):
        return "feishu connection failed"
    if isinstance(exc, httpx.TimeoutException):
        return "feishu request timed out; delivery outcome unknown"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"feishu returned HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.RequestError):
        return "feishu transport error"
    message = str(exc)
    # Belt-and-suspenders: drop anything that looks like a URL or secret.
    if "http" in message or "open.feishu" in message or "sign" in message.lower():
        return "feishu delivery error"
    return message[:200]


class FeishuService:
    """Send Feishu cards with structured, privacy-safe outcomes."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.notifier = FeishuNotifier(
            webhook_env=settings.feishu.webhook_env,
            secret_env=settings.feishu.secret_env,
            max_items_per_section=settings.feishu.max_items_per_section,
            redact_confidential=settings.feishu.redact_confidential,
            retry_attempts=settings.feishu.retry_attempts,
        )

    # -- configuration ---------------------------------------------------- #
    def runtime_state(self) -> str:
        """Mirror ``validate_feishu_runtime_config`` for callers holding settings."""
        return validate_feishu_runtime_config(self.settings)

    # -- sending ----------------------------------------------------------- #
    def send(self, message: dict, *, timeout: float = 30) -> DeliveryResult:
        """POST one message and return a structured ``DeliveryResult``."""
        state = self.runtime_state()
        if state == "disabled":
            return DeliveryResult("disabled", False, error_code="feishu_disabled",
                                  safe_error="Feishu notifications are disabled")
        if state != "ready":
            safe_error = {
                "missing_webhook": "Feishu webhook not configured",
                "missing_secret": "Feishu signing secret not configured",
                "invalid_webhook": "Feishu webhook URL is invalid",
            }.get(state, "Feishu misconfigured")
            return DeliveryResult(state, False, error_code=state, safe_error=safe_error)

        webhook = self.notifier.webhook_url
        timestamp = int(time.time())
        sign = self.notifier._sign(timestamp)
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(
                    webhook,
                    json={"timestamp": timestamp, "sign": sign, **message},
                )
            if response.status_code == 429:
                return DeliveryResult("rate_limited", True, error_code="http_429",
                                      safe_error="Feishu rate limited")
            response.raise_for_status()
            body = response.json()
            if isinstance(body, dict) and body.get("code") not in (0, None):
                code = body.get("code")
                retryable = code not in NON_RETRYABLE_BUSINESS_CODES
                status = "rejected" if not retryable else "server_failed"
                return DeliveryResult(
                    status, retryable,
                    error_code="feishu_business_error",
                    provider_code=str(code),
                    safe_error="Feishu rejected the message",
                )
            return DeliveryResult("sent", False)
        except httpx.TimeoutException as exc:
            # Outcome is unknown: do NOT auto-retry to avoid duplicate pushes.
            return DeliveryResult("unknown_outcome", False, safe_error=sanitize_feishu_error(exc))
        except httpx.ConnectError as exc:
            return DeliveryResult("connect_failed", True, safe_error=sanitize_feishu_error(exc))
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code >= 500:
                return DeliveryResult("server_failed", True, error_code=f"http_{status_code}",
                                      safe_error=sanitize_feishu_error(exc))
            return DeliveryResult("rejected", False, error_code=f"http_{status_code}",
                                  safe_error=sanitize_feishu_error(exc))
        except httpx.RequestError as exc:
            return DeliveryResult("connect_failed", True, safe_error=sanitize_feishu_error(exc))
        except Exception as exc:  # noqa: BLE001 - we sanitize before recording
            return DeliveryResult("unknown_outcome", False, safe_error=sanitize_feishu_error(exc))

    def send_pending_summary(self, summary, **fields) -> DeliveryResult:
        """Build and send a single Pending summary card.

        Content controls from ``settings.feishu`` (content_mode, character caps,
        risk-item cap, detail-link toggle) are forwarded to the card builder so
        no full body / attachment content ever leaves the machine (§3.5).
        """
        fields = {
            "content_mode": self.settings.feishu.content_mode,
            "max_summary_chars": self.settings.feishu.max_summary_chars,
            "max_action_chars": self.settings.feishu.max_action_chars,
            "max_risk_items": self.settings.feishu.max_risk_items,
            "include_detail_link": self.settings.feishu.include_detail_link,
            **fields,
        }
        message = self.notifier._build_pending_summary_message(summary, **fields)
        return self.send(message)

    def send_test(self, *, timeout: float = 30) -> DeliveryResult:
        """Send the synthetic connectivity-test card (plan-0805-02 §3.6)."""
        message = {
            "msg_type": "text",
            "content": {"text": "OARadar 飞书连接测试"},
        }
        return self.send(message, timeout=timeout)


def is_retryable(result: DeliveryResult) -> bool:
    """True when an automatic retry of ``result`` is safe (plan-0805-02 §3.2)."""
    return result.retryable and result.status in RETRYABLE_STATUSES


def delivery_status_for_result(result: DeliveryResult) -> str:
    """Map a ``DeliveryResult`` to a ``NotificationDelivery.status`` value.

    Sent is terminal-success; rejected/misconfigured are terminal-failure;
    retryable transport errors enter ``retry_wait``; any other non-sent outcome
    (e.g. ``unknown_outcome``) is parked as ``unknown`` for manual retry.
    """
    if result.status == "sent":
        return "sent"
    if result.status in {"rejected", "misconfigured"}:
        return "failed"
    if result.retryable:
        return "retry_wait"
    return "unknown"


def apply_delivery_result(delivery, result: DeliveryResult, now: datetime) -> None:
    """Mutate a ``NotificationDelivery`` in place from a ``DeliveryResult``.

    Pure (no DB commit) so the worker and the manual retry path share it.
    """
    delivery.status = delivery_status_for_result(result)
    delivery.attempts = (delivery.attempts or 0) + 1
    delivery.error_code = result.error_code
    delivery.last_error = sanitize_feishu_error_value(result.safe_error)
    if result.status == "sent":
        delivery.sent_at = now
        delivery.error_code = None
        delivery.last_error = None
        delivery.next_retry_at = None
    elif result.retryable:
        delivery.next_retry_at = now + timedelta(minutes=min(30, 2 ** (delivery.attempts or 1)))
    else:
        delivery.next_retry_at = None


def sanitize_feishu_error_value(value: str | None) -> str | None:
    """Collapse a safe-error string to something log/DB safe (no URL/secret)."""
    if not value:
        return None
    if "http" in value or "open.feishu" in value or "sign" in value.lower():
        return "feishu delivery error"
    return value[:200]


def retry_pending_summary_delivery(
    engine, settings: Settings, delivery_id: int, *, now: datetime | None = None
) -> DeliveryResult:
    """Re-send a ``pending_summary`` delivery by id (plan-0805-02 §3.6).

    Rebuilds the card from the current summary + latest occurrence and records
    the outcome on the same ``NotificationDelivery`` row. Used by the
    ``oa notifications retry`` command and the Stage 6 API.
    """
    from oa_knowledge.collector.pending import PendingAdapter
    from oa_knowledge.db.models import ItemOccurrence, NotificationDelivery, SummaryVersion
    from oa_knowledge.pending_summary import PendingSummary
    from sqlalchemy import select

    now = now or datetime.now(timezone.utc)
    state = validate_feishu_runtime_config(settings)
    if state != "ready":
        return DeliveryResult(state, False, error_code=state, safe_error="Feishu misconfigured")

    from sqlalchemy.orm import Session  # noqa: PLC0415 - keep the service import-light

    with Session(engine) as session:
        delivery = session.get(NotificationDelivery, delivery_id)
        if delivery is None:
            return DeliveryResult("rejected", False, error_code="delivery_not_found",
                                  safe_error="delivery record not found")
        if delivery.notification_type != "pending_summary":
            return DeliveryResult("rejected", False, error_code="unsupported_type",
                                  safe_error="only pending_summary deliveries can be retried")
        version = session.scalar(select(SummaryVersion).where(
            SummaryVersion.logical_item_id == delivery.logical_item_id,
            SummaryVersion.summary_kind == "pending",
            SummaryVersion.status == "current",
        ).order_by(SummaryVersion.version.desc()).limit(1))
        if version is None:
            return DeliveryResult("rejected", False, error_code="summary_missing",
                                  safe_error="no current summary to resend")
        summary = PendingSummary.model_validate_json(version.structured_json)
        occurrence = session.scalar(select(ItemOccurrence).where(
            ItemOccurrence.logical_item_id == delivery.logical_item_id,
            ItemOccurrence.channel == "pending",
        ).order_by(ItemOccurrence.id.desc()).limit(1))
        title = (occurrence.title if occurrence else None) or summary.matter_type or f"待办 {delivery.logical_item_id}"
        sender = occurrence.sender if occurrence else None
        current_node = occurrence.current_node if occurrence else None
        deadline_text = occurrence.deadline_text if occurrence else None
        detail_url = occurrence.detail_url if (occurrence and occurrence.detail_url) else None
        if not detail_url and occurrence and occurrence.affair_id_text:
            detail_url = PendingAdapter.detail_url(settings.browser.base_url, occurrence.affair_id_text)

        service = FeishuService(settings)
        result = service.send_pending_summary(
            summary,
            title=title,
            sender=sender or "",
            current_node=current_node or "",
            deadline_text=deadline_text or "",
            detail_url=detail_url or "",
        )
        # Re-load and mutate so we work on the same identity as the outer session.
        delivery = session.get(NotificationDelivery, delivery_id)
        delivery.channel = "feishu"
        delivery.notification_type = "pending_summary"
        apply_delivery_result(delivery, result, now)
        session.commit()
        return result
