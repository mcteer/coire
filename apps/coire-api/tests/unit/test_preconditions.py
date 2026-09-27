from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from coire_api.preconditions import require_current, require_version


def test_version_precondition_accepts_quoted_utc_timestamp() -> None:
    require_current('"2026-09-01T00:00:00Z"', datetime(2026, 9, 1, tzinfo=UTC))


@pytest.mark.parametrize("value, status_code", [(None, 428), ("garbage", 400)])
def test_version_precondition_refuses_missing_or_invalid_values(
    value: str | None, status_code: int
) -> None:
    with pytest.raises(HTTPException) as caught:
        require_version(value)
    assert caught.value.status_code == status_code


def test_version_precondition_reports_conflict_and_current_version() -> None:
    current = datetime(2026, 9, 1, tzinfo=UTC)
    with pytest.raises(HTTPException) as caught:
        require_current("2026-08-31T00:00:00Z", current)
    assert caught.value.status_code == 409
    detail: object = caught.value.detail
    assert detail == {
        "code": "edit_conflict",
        "current_version": "2026-09-01T00:00:00+00:00",
    }
