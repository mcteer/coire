"""Align HTTP housekeeping wakeups across the Studio's existing listeners."""

from __future__ import annotations

import asyncio
import math

import uvicorn


async def _wait_for_http_tick() -> None:
    loop = asyncio.get_running_loop()
    # Keep Uvicorn's 100-ms exit/request-limit checks. Absolute common deadlines
    # let control and data listeners share a kernel wakeup instead of alternating.
    deadline = (math.floor(loop.time() * 10) + 1) / 10
    ready: asyncio.Future[None] = loop.create_future()

    def finish_tick() -> None:
        if not ready.done():
            ready.set_result(None)

    timer = loop.call_at(deadline, finish_tick)
    try:
        await ready
    finally:
        timer.cancel()


class NodeHTTPServer(uvicorn.Server):
    async def main_loop(self) -> None:
        counter = 0
        while not await self.on_tick(counter):
            counter = (counter + 1) % 864000
            await _wait_for_http_tick()
