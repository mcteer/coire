from __future__ import annotations

import asyncio

import pytest

from coire_api.polling import FailureSummary, PollBackoff, wait_or_stop


def test_idle_failure_and_active_delays_are_independent_and_bounded() -> None:
    poll = PollBackoff(0.5, 4, 8)
    assert [poll.idle() for _ in range(6)] == [0.5, 1, 2, 4, 4, 4]
    assert [poll.failed() for _ in range(6)] == [0.5, 1, 2, 4, 8, 8]
    poll.active()
    assert poll.idle() == 0.5
    assert poll.failed() == 0.5


@pytest.mark.asyncio
async def test_stop_wakes_long_backoff_immediately() -> None:
    stop = asyncio.Event()
    waiter = asyncio.create_task(wait_or_stop(stop, 60))
    stop.set()
    await asyncio.wait_for(waiter, timeout=0.1)


def test_failure_summary_limits_logs_without_slipping_poll_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr("coire_api.polling.time.monotonic", lambda: clock[0])
    summary = FailureSummary(interval_s=30)
    assert summary.failed() == 0
    for tick in range(1, 30):
        clock[0] = float(tick)
        assert summary.failed() is None
    clock[0] = 30.0
    assert summary.failed() == 29
    summary.succeeded()
    assert summary.failed() == 0
