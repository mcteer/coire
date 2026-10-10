"""Baseline telemetry remains bounded and emits no contributor content."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from coire_api.feedback import baseline


async def test_baseline_emits_closed_state_zeroes_and_sets_heartbeat_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    rows = MagicMock()
    rows.tuples.return_value.all.return_value = [("queued", 2)]
    session.execute.return_value = rows
    session.scalar.side_effect = [1, 0]
    states, overdue, pending, heartbeat = [MagicMock() for _ in range(4)]
    monkeypatch.setattr(baseline, "export_states", states)
    monkeypatch.setattr(baseline, "export_overdue", overdue)
    monkeypatch.setattr(baseline, "export_cleanup_pending", pending)
    monkeypatch.setattr(baseline, "snapshot_timestamp", heartbeat)
    monkeypatch.setattr(baseline, "record_purge_age", AsyncMock(return_value=0))
    await baseline.record_feedback_metrics(session)
    assert states.set.call_count == 6
    assert states.set.call_args_list[0].args[0] == 2
    assert states.set.call_args_list[-1].args[0] == 0
    assert overdue.set.call_args.args == (1,)
    assert pending.set.call_args.args == (0,)
    assert heartbeat.set.call_count == 1
    query = str(session.execute.call_args.args[0])
    assert "prompt" not in query and "candidate" not in query and "request" not in query


async def test_failed_baseline_never_manufactures_a_fresh_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    session.execute.side_effect = RuntimeError("private data")
    heartbeat = MagicMock()
    monkeypatch.setattr(baseline, "snapshot_timestamp", heartbeat)
    with pytest.raises(RuntimeError):
        await baseline.record_feedback_metrics(session)
    heartbeat.set.assert_not_called()
