"""Committed image cancellation has a short scheduler discovery bound."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace

import pytest
from dbos import DBOS

from coire_scheduler import main
from coire_scheduler.images import image_cancel_workflow


@pytest.mark.parametrize("ids", [[], ["01J00000000000000000000000"]])
async def test_cancel_poller_dispatches_fenced_workflow_within_scan_bound(
    monkeypatch: pytest.MonkeyPatch, ids: list[str]
) -> None:
    calls: list[str] = []
    delays: list[float] = []

    class Session:
        async def execute(self, statement: object) -> SimpleNamespace:
            query = str(statement.compile(compile_kwargs={"literal_binds": True}))  # type: ignore[attr-defined]
            assert "image_jobs.state" in query and "cancelling" in query
            return SimpleNamespace(scalars=lambda: ids)

    @asynccontextmanager
    async def scope() -> AsyncIterator[Session]:
        yield Session()

    def start(workflow: object, job_id: str) -> None:
        assert workflow is image_cancel_workflow
        calls.append(job_id)

    async def wait(stop: asyncio.Event, delay: float) -> None:
        delays.append(delay)
        stop.set()

    monkeypatch.setattr(main, "session_scope", scope)
    monkeypatch.setattr(DBOS, "start_workflow", start)
    monkeypatch.setattr(main, "SetWorkflowID", lambda workflow_id: nullcontext())
    monkeypatch.setattr(main, "wait_or_stop", wait)
    stop = asyncio.Event()
    await main.dispatch_image_cancels(stop)
    assert calls == ids
    assert delays == [0.25]
