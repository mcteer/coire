"""Measurement control cannot resurrect a terminal evaluation or miss its stream event."""

import uuid
from unittest.mock import AsyncMock

import pytest

from coire_api.db import EvaluationMeasurementRow, EvaluationRunRow
from coire_core.models.evaluation import EvaluationControl


@pytest.mark.parametrize("state", ["running", "succeeded", "failed", "cancelled", "timed_out"])
async def test_measurement_cancel_preserves_terminal_run_and_records_active_stop(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    import coire_api.evaluation.measurements as module

    run = EvaluationRunRow(id="01ARZ3NDEKTSV4RRFFQ69G5FAV", state=state, version=3)
    row = EvaluationMeasurementRow(
        id=uuid.uuid4(), state="running", version=1, execution={"run_id": run.id}
    )
    session = AsyncMock()
    session.get.side_effect = [row, run]
    monkeypatch.setattr(module, "authorize_live_evaluation_action", AsyncMock())
    monkeypatch.setattr(module, "write_principal_audit", AsyncMock())
    monkeypatch.setattr(module, "project", lambda row: row)
    stop = AsyncMock()
    event = AsyncMock()
    monkeypatch.setattr("coire_api.evaluation.execution.stop_children", stop)
    monkeypatch.setattr("coire_api.evaluation.events.append", event)
    await module.cancel_measurement(
        session, AsyncMock(), row.id, EvaluationControl(expected_version=1)
    )
    assert row.state == "cancelled"
    if state == "running":
        assert run.state == "cancelling" and run.version == 4
        stop.assert_awaited_once_with(session, run.id)
        event.assert_awaited_once_with(session, run)
    else:
        assert run.state == state and run.version == 3
        event.assert_not_awaited()
        stop.assert_not_awaited()
