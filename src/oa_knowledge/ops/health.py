from __future__ import annotations

from datetime import UTC, datetime


def utc_age_hours(timestamp: str, *, now: datetime | None = None) -> float:
    """Return the UTC age of a persisted timestamp in hours."""

    observed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return (current - observed_at).total_seconds() / 3600
