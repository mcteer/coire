"""Group replay has an independent bounded durable cursor and no raw outputs."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from coire_core.models.evaluation import EvaluationGroupEvent, EvaluationGroupReplayPage


def test_group_event_rejects_raw_text_and_invalid_cursor() -> None:
    body = {
        "sequence": 1,
        "group_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "kind": "state",
        "state": "pending",
        "created_at": datetime.now(UTC),
    }
    event = EvaluationGroupEvent.model_validate(body)
    assert EvaluationGroupReplayPage(events=[event], cursor=1).cursor == 1
    for changed in ({"outputs": ["private"]}, {"sequence": 0}, {"state": "invented"}):
        with pytest.raises(ValidationError):
            EvaluationGroupEvent.model_validate({**body, **changed})
