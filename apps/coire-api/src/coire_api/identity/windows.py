"""UTC fixed-window boundaries used for rate and monthly quota accounting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("accounting timestamps must be timezone-aware")
    return current.astimezone(UTC)


def minute_window(value: datetime | None = None) -> tuple[datetime, datetime]:
    start = utc(value).replace(second=0, microsecond=0)
    return start, start + timedelta(minutes=1)


def month_window(value: datetime | None = None) -> tuple[datetime, datetime]:
    current = utc(value)
    start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end
