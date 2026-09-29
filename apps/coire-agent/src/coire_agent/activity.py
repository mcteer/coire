"""Content-free, bounded activity receipts in an isolated coding run output mount."""

from __future__ import annotations

import os
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Literal

from coire_core.models.runs import (
    RUN_ACTIVITY_MAX_BYTES,
    RUN_ACTIVITY_MAX_RECORDS,
    RUN_ACTIVITY_TOOL_NAMES,
    RunActivity,
    RunActivityTool,
)

_MARKER_RESERVE = 512


class ActivitySpool:
    def __init__(
        self,
        path: Path,
        run_id: uuid.UUID,
        *,
        max_bytes: int = RUN_ACTIVITY_MAX_BYTES,
        max_records: int = RUN_ACTIVITY_MAX_RECORDS,
    ) -> None:
        if max_bytes < 2048 or not 2 <= max_records <= RUN_ACTIVITY_MAX_RECORDS:
            raise ValueError("activity spool limits are invalid")
        self.path = path
        self.run_id = run_id
        self.max_bytes = max_bytes
        self.max_records = max_records
        self.sequence = 0
        self.bytes_written = 0
        self.truncated = False

    def _write(self, record: RunActivity) -> None:
        payload = record.model_dump_json().encode() + b"\n"
        descriptor = os.open(
            self.path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("activity spool must be a regular file")
            if os.fstat(descriptor).st_size + len(payload) > self.max_bytes:
                raise ValueError("activity spool byte limit exceeded")
            view = memoryview(payload)
            while view:
                view = view[os.write(descriptor, view) :]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.sequence = record.sequence
        self.bytes_written += len(payload)

    def _overflow(self) -> None:
        if self.truncated:
            return
        self.truncated = True
        marker = RunActivity(
            run_id=self.run_id,
            sequence=self.sequence + 1,
            tool_name=RunActivityTool.ACTIVITY_SPOOL,
            state="failed",
            created_at=datetime.now(UTC),
            safe_error="limit_reached",
        )
        self._write(marker)

    def record(
        self,
        tool_name: str,
        state: Literal["started", "completed", "failed"],
        *,
        duration_ms: int | None = None,
        safe_error: str | None = None,
        tool_call_id: uuid.UUID | None = None,
    ) -> None:
        if tool_name not in RUN_ACTIVITY_TOOL_NAMES or tool_name == "activity_spool":
            raise ValueError("activity tool name is not allowed")
        if safe_error not in {None, "operation_failed"}:
            raise ValueError("activity error code is not allowed")
        if self.truncated:
            return
        record = RunActivity(
            run_id=self.run_id,
            sequence=self.sequence + 1,
            tool_name=RunActivityTool(tool_name),
            tool_call_id=tool_call_id,
            state=state,
            created_at=datetime.now(UTC),
            duration_ms=duration_ms,
            safe_error=safe_error,
        )
        length = len(record.model_dump_json().encode()) + 1
        if (
            self.sequence + 1 >= self.max_records
            or self.bytes_written + length + _MARKER_RESERVE > self.max_bytes
        ):
            self._overflow()
            return
        self._write(record)

    @contextmanager
    def step(self, tool_name: str) -> Iterator[None]:
        tool_call_id = uuid.uuid4()
        self.record(tool_name, "started", tool_call_id=tool_call_id)
        started = monotonic()
        try:
            yield
        except BaseException:
            self.record(
                tool_name,
                "failed",
                duration_ms=max(0, int((monotonic() - started) * 1000)),
                safe_error="operation_failed",
                tool_call_id=tool_call_id,
            )
            raise
        else:
            self.record(
                tool_name,
                "completed",
                duration_ms=max(0, int((monotonic() - started) * 1000)),
                tool_call_id=tool_call_id,
            )
