"""Baseline state remains visible when admission and diagnostics are disabled."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession


async def test_idle_baseline_emits_explicit_zeroes_and_failure_preserves_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from contextlib import asynccontextmanager

    from coire_scheduler import evaluation_metrics as module

    session = AsyncMock()
    session.execute.return_value = Mock(all=Mock(return_value=[]))
    session.scalar.return_value = None
    now = datetime.now(UTC)
    value = await module.load_evaluation_metrics(session, now=now)
    assert value.timestamp == now.timestamp()
    assert value.cleanup_overdue == 0 and value.pending_oldest == 0
    assert all(count == 0 for count in value.runs.values())
    output = module.EvaluationBaselineMetrics(Mock())
    output.publish(value)
    assert next(iter(output.callback("cleanup_overdue")(None))).value == 0

    stop = asyncio.Event()

    @asynccontextmanager
    async def unavailable() -> AsyncIterator[AsyncSession]:
        if not stop.is_set():
            raise RuntimeError("private SQL must not be logged")
        yield session

    async def end(*args: object) -> None:
        stop.set()

    monkeypatch.setattr(module, "session_scope", unavailable)
    monkeypatch.setattr(module, "wait_or_stop", end)
    await module.poll_evaluation_metrics(stop, emitter=output)
    assert output.snapshot is value


async def test_persisted_cleanup_and_pending_ages_are_visible() -> None:
    from coire_scheduler.evaluation_metrics import load_evaluation_metrics

    now = datetime.now(UTC)
    session = AsyncMock()
    session.execute.return_value = Mock(all=Mock(return_value=[("running", 1), ("failed", 2)]))
    session.scalar.side_effect = [now - timedelta(seconds=120), now - timedelta(seconds=301)]
    snapshot = await load_evaluation_metrics(session, now=now)
    assert snapshot.cleanup_overdue == 120
    assert snapshot.pending_oldest == 301
    assert snapshot.runs["running"] == 1
