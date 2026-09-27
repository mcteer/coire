from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from coire_core.models.console import ActivityItem, ActivityKind, AskRequest, CursorPage


def test_cursor_page_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CursorPage[str].model_validate({"items": [], "secret": "no"})


def test_ask_question_is_bounded() -> None:
    assert AskRequest(question="cluster status?").question == "cluster status?"
    with pytest.raises(ValidationError):
        AskRequest(question="")


def test_cursor_page_serializes_timestamp_items() -> None:
    now = datetime.now(UTC)
    assert CursorPage[datetime](items=[now]).model_dump(mode="json")["items"] == [
        now.isoformat().replace("+00:00", "Z")
    ]


def test_activity_contract_bounds_progress() -> None:
    import uuid

    with pytest.raises(ValidationError):
        ActivityItem(
            id=uuid.uuid4(),
            kind=ActivityKind.JOB,
            owner="admin",
            target="model",
            state="pull",
            started_at=datetime.now(UTC),
            elapsed_seconds=1,
            progress_percent=101,
            can_stop=True,
        )
