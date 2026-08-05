"""Structured Feishu delivery result types (plan-0805-02 §3.1)."""
from __future__ import annotations

from dataclasses import dataclass

# Delivery lifecycle statuses recorded on NotificationDelivery.
DELIVERY_STATUS = (
    "queued",
    "sending",
    "sent",
    "retry_wait",
    "unknown",
    "failed",
    "skipped_disabled",
)

# send() outcome classifications returned by FeishuService.
SEND_STATUS = (
    "sent",
    "rejected",
    "connect_failed",
    "rate_limited",
    "server_failed",
    "unknown_outcome",
    "disabled",
    "misconfigured",
)


@dataclass(frozen=True)
class DeliveryResult:
    """Structured outcome of one Feishu send attempt.

    ``retryable`` tells the caller whether an automatic retry is safe. A read
    timeout is classified ``unknown_outcome`` with ``retryable=False`` by
    default, because the message may already have been delivered and a blind
    retry would push a duplicate (plan-0805-02 §3.2).
    """

    status: str
    retryable: bool
    error_code: str | None = None
    safe_error: str | None = None
    provider_code: str | None = None


# Retry classification table (plan-0805-02 §3.2).
RETRYABLE_STATUSES = {"connect_failed", "rate_limited", "server_failed"}
