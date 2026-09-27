"""Scheduler worker ownership and bounded unwind."""

from __future__ import annotations

import asyncio

import pytest

from coire_core.settings import Settings
from coire_scheduler.workers import SchedulerWorkers


def test_scheduler_declares_seven_workers() -> None:
    settings = Settings(_secrets_dir="/none")  # type: ignore[call-arg]
    supervisor = SchedulerWorkers(settings)
    assert len(supervisor.workers) == 7
    assert supervisor.kill_executor.external_kill_scan is True


@pytest.mark.asyncio
async def test_partial_startup_stops_previously_started_workers() -> None:
    supervisor = SchedulerWorkers(Settings(_secrets_dir="/none"))  # type: ignore[call-arg]
    events: list[str] = []

    class Good:
        async def start(self) -> None:
            events.append("start-good")

        async def stop(self) -> None:
            events.append("stop-good")

    class Bad:
        async def start(self) -> None:
            raise RuntimeError("startup failed")

        async def stop(self) -> None:
            raise AssertionError("never started")

    supervisor.workers = [Good(), Bad()]
    with pytest.raises(RuntimeError, match="startup failed"):
        await supervisor.start()
    assert events == ["start-good", "stop-good"]
    assert supervisor.started == []


@pytest.mark.asyncio
async def test_shutdown_cancels_stuck_worker() -> None:
    supervisor = SchedulerWorkers(Settings(_secrets_dir="/none"))  # type: ignore[call-arg]
    supervisor.shutdown_timeout_s = 0.02
    cancelled = asyncio.Event()

    class Stuck:
        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()

    supervisor.workers = [Stuck()]
    await supervisor.start()
    await asyncio.wait_for(supervisor.stop(), 0.5)
    assert cancelled.is_set()
