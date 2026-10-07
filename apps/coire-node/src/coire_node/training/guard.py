"""Deadline decisions shared by the worker control lane and node-local watchdog."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from coire_core.errors import TrainingConflict


class ExecutionGuard:
    """Monotonic deadlines: wall-clock rollback cannot extend an execution lease."""

    def __init__(
        self, expires_at: datetime, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.clock = clock
        self.lock = threading.RLock()
        self.lease_deadline = 0.0
        self.pause_deadline: float | None = None
        self.cancel_deadline: float | None = None
        self.renew(expires_at)

    def renew(self, expires_at: datetime) -> None:
        seconds = (expires_at - datetime.now(UTC)).total_seconds()
        if not math.isfinite(seconds) or not 0 < seconds <= 30:
            raise TrainingConflict("Execution lease must expire within thirty seconds")
        with self.lock:
            if self.lease_deadline and self.clock() >= self.lease_deadline:
                raise TrainingConflict("Expired execution lease cannot be resurrected")
            deadline = self.clock() + seconds
            if deadline < self.lease_deadline:
                raise TrainingConflict("Execution lease cannot move backwards")
            self.lease_deadline = deadline

    def pause(self) -> None:
        with self.lock:
            if self.pause_deadline is None:
                self.pause_deadline = self.clock() + 60

    def cancel(self) -> None:
        with self.lock:
            if self.cancel_deadline is None:
                self.cancel_deadline = self.clock() + 5

    def action(self) -> str:
        with self.lock:
            now = self.clock()
            if now >= self.lease_deadline:
                return "kill"
            if self.cancel_deadline is not None:
                return "kill" if now >= self.cancel_deadline else "cancel"
            if self.pause_deadline is not None:
                return "kill" if now >= self.pause_deadline else "pause"
            return "continue"
