"""Bounded, stop-aware delays for ordinary background queues."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress


class PollBackoff:
    def __init__(self, base_s: float, idle_max_s: float, failure_max_s: float) -> None:
        self.base_s = base_s
        self.idle_max_s = idle_max_s
        self.failure_max_s = failure_max_s
        self.idle_s = base_s
        self.failure_s = base_s

    def active(self) -> None:
        self.idle_s = self.base_s
        self.failure_s = self.base_s

    def idle(self) -> float:
        delay = self.idle_s
        self.idle_s = min(self.idle_max_s, max(self.base_s, self.idle_s * 2))
        self.failure_s = self.base_s
        return delay

    def failed(self) -> float:
        delay = self.failure_s
        self.failure_s = min(self.failure_max_s, max(self.base_s, self.failure_s * 2))
        return delay


async def wait_or_stop(stop: asyncio.Event, delay_s: float) -> None:
    with suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=delay_s)


class FailureSummary:
    """Retain fast safety polling without repeating identical error logs each tick."""

    def __init__(self, interval_s: float = 30.0) -> None:
        self.interval_s = interval_s
        self.last_logged = float("-inf")
        self.suppressed = 0

    def failed(self) -> int | None:
        now = time.monotonic()
        if now - self.last_logged < self.interval_s:
            self.suppressed += 1
            return None
        count = self.suppressed
        self.last_logged = now
        self.suppressed = 0
        return count

    def succeeded(self) -> None:
        self.last_logged = float("-inf")
        self.suppressed = 0
