"""Pin sweeping remains active after admission closes and requires settled ownership."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import EvaluationCheckpointPinRow, TrainingEvaluationTriggerRow, TrainingJobRow
from coire_core.models.auth import UserRole


@pytest.mark.parametrize(
    "blocked_by", [None, "unfinished", "pause_owner", "active_run", "cleanup", "adapter"]
)
async def test_completed_pin_release_requires_safe_trigger_and_group_cleanup(
    monkeypatch: pytest.MonkeyPatch, blocked_by: str | None
) -> None:
    from coire_api.evaluation.cleanup import release_trigger_pins

    trigger_id = uuid.uuid4()
    owner = uuid.uuid4()
    trigger = TrainingEvaluationTriggerRow(
        id=trigger_id,
        job_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        group_id="01ARZ3NDEKTSV4RRFFQ69G5FAW",
        phase="evaluating" if blocked_by == "unfinished" else "complete",
        completed_at=datetime.now(UTC),
    )
    job = TrainingJobRow(
        id=trigger.job_id,
        owner_user_id=owner,
        evaluation_pause_trigger_id=trigger_id if blocked_by == "pause_owner" else None,
        authorization_snapshot=Principal(
            kind=PrincipalKind.USER, user_id=owner, role=UserRole.ADMIN
        ).model_dump(mode="json"),
    )
    pin = EvaluationCheckpointPinRow(
        id=uuid.uuid4(), trigger_id=trigger_id, checkpoint_id=uuid.uuid4(), released_at=None
    )
    session = AsyncMock()
    session.get.side_effect = [trigger, job, trigger]
    session.scalar.side_effect = [
        "unsafe" if blocked_by in {"active_run", "cleanup"} else None,
        "unsafe" if blocked_by == "adapter" else None,
    ]
    session.scalars.return_value.all = lambda: [pin]
    audit = AsyncMock()
    monkeypatch.setattr("coire_api.evaluation.cleanup.write_principal_audit", audit)
    now = datetime.now(UTC)
    count = await release_trigger_pins(session, trigger_id, now=now)
    if blocked_by is None:
        assert count == 1 and pin.released_at == now
        audit.assert_awaited_once()
    else:
        assert count == 0 and pin.released_at is None
        audit.assert_not_awaited()
