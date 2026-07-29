from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class BackfillWindow:
    start: date
    end: date
    granularity: str


def next_month_remainder(range_start: date, cursor_end: date) -> BackfillWindow | None:
    if cursor_end <= range_start:
        return None
    probe = cursor_end - timedelta(days=1)
    month_start = date(probe.year, probe.month, 1)
    start = max(range_start, month_start)
    days = (cursor_end - start).days
    granularity = "month" if start == month_start and days >= 28 else "half_month" if days > 7 else "week"
    return BackfillWindow(start, cursor_end, granularity)


def shrink_latest(window: BackfillWindow) -> BackfillWindow:
    days = (window.end - window.start).days
    if days > 16:
        probe = window.end - timedelta(days=1)
        middle = date(probe.year, probe.month, 16)
        if middle <= window.start or middle >= window.end:
            middle = window.end - timedelta(days=15)
        return BackfillWindow(middle, window.end, "half_month")
    if days > 7:
        return BackfillWindow(window.end - timedelta(days=7), window.end, "week")
    raise ValueError("a seven-day OA backfill window exceeds 500 items; manual review required")
