"""Listener wakeups coalesce while Uvicorn's exit and notification checks remain intact."""

import asyncio
from collections.abc import Callable
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi import FastAPI

from coire_node import http_server


async def test_listener_ticks_share_deadlines_without_lengthening_exit_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual = asyncio.get_running_loop()
    readings = iter((0.251, 0.257, 0.300))
    deadlines: list[float] = []
    cancellations: list[bool] = []

    class Clock:
        def time(self) -> float:
            return next(readings)

        def create_future(self) -> asyncio.Future[None]:
            return actual.create_future()

        def call_at(self, when: float, callback: Callable[[], None]) -> SimpleNamespace:
            deadlines.append(when)
            callback()
            return SimpleNamespace(cancel=lambda: cancellations.append(True))

    clock = Clock()
    with monkeypatch.context() as patch:
        patch.setattr(asyncio, "get_running_loop", lambda: clock)
        for _ in range(3):
            await http_server._wait_for_http_tick()
    assert deadlines == [0.3, 0.3, 0.4]
    assert cancellations == [True, True, True]


async def test_programmatic_exit_retains_subsecond_detection() -> None:
    server = http_server.NodeHTTPServer(uvicorn.Config(FastAPI(), ws="none", lifespan="off"))
    server.config.load()
    running = asyncio.create_task(server.main_loop())
    try:
        await asyncio.sleep(0)
        server.should_exit = True
        await asyncio.wait_for(running, timeout=0.5)
    finally:
        running.cancel()


async def test_date_headers_and_notify_callback_keep_once_per_second_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notifications: list[bool] = []

    async def notify() -> None:
        notifications.append(True)

    server = http_server.NodeHTTPServer(
        uvicorn.Config(
            FastAPI(), ws="none", lifespan="off", callback_notify=notify, timeout_notify=0
        )
    )
    server.config.load()
    original = server.on_tick
    counters: list[int] = []

    async def on_tick(counter: int) -> bool:
        counters.append(counter)
        await original(counter)
        return counter == 10

    monkeypatch.setattr(server, "on_tick", on_tick)
    await asyncio.wait_for(server.main_loop(), timeout=2)
    assert counters == list(range(11))
    assert len(notifications) == 2
    assert any(name == b"date" for name, _ in server.server_state.default_headers)
