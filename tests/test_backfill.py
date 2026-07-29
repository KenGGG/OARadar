from datetime import date

import pytest

from oa_knowledge.backfill import BackfillWindow, next_month_remainder, shrink_latest


def test_backfill_moves_latest_to_oldest_by_month_remainder() -> None:
    first = next_month_remainder(date(2019, 1, 1), date(2026, 1, 1))
    assert first == BackfillWindow(date(2025, 12, 1), date(2026, 1, 1), "month")
    remainder = next_month_remainder(date(2019, 1, 1), date(2025, 12, 16))
    assert remainder == BackfillWindow(date(2025, 12, 1), date(2025, 12, 16), "half_month")


def test_backfill_shrinks_month_to_half_then_week() -> None:
    half = shrink_latest(BackfillWindow(date(2025, 12, 1), date(2026, 1, 1), "month"))
    assert half == BackfillWindow(date(2025, 12, 16), date(2026, 1, 1), "half_month")
    week = shrink_latest(half)
    assert week == BackfillWindow(date(2025, 12, 25), date(2026, 1, 1), "week")
    with pytest.raises(ValueError):
        shrink_latest(week)
