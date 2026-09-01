from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coire_api.identity.windows import minute_window, month_window


def test_minute_window_is_fixed_utc() -> None:
    start, end = minute_window(datetime(2026, 8, 31, 23, 59, 59, 999, tzinfo=UTC))
    assert start == datetime(2026, 8, 31, 23, 59, tzinfo=UTC)
    assert end == datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


def test_month_window_handles_leap_and_year_rollover() -> None:
    assert month_window(datetime(2028, 2, 29, tzinfo=UTC)) == (
        datetime(2028, 2, 1, tzinfo=UTC),
        datetime(2028, 3, 1, tzinfo=UTC),
    )
    assert month_window(datetime(2026, 12, 31, tzinfo=UTC)) == (
        datetime(2026, 12, 1, tzinfo=UTC),
        datetime(2027, 1, 1, tzinfo=UTC),
    )


def test_naive_accounting_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        month_window(datetime(2026, 1, 1))
