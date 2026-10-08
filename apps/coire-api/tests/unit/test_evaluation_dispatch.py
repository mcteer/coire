"""Default-off recovery is active without creating an unused private volume namespace."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_core.settings import Settings


async def test_disabled_empty_dispatch_does_not_initialize_evidence_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import coire_scheduler.main as module

    session = AsyncMock(spec=AsyncSession)
    session.scalars.return_value.all = lambda: []
    session.scalar.return_value = None

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        yield session

    store = Mock(side_effect=AssertionError("unused evidence namespace must remain untouched"))
    stop = asyncio.Event()

    async def finish(stop_event: asyncio.Event, seconds: float) -> None:
        stop_event.set()

    monkeypatch.setattr(module, "session_scope", scope)
    monkeypatch.setattr(module, "get_settings", lambda: Settings(evaluations_enabled=False))
    monkeypatch.setattr(module, "wait_or_stop", finish)
    monkeypatch.setattr("coire_api.evaluation.evidence.EvidenceStore", store)
    await module.dispatch_evaluations(stop)
    store.assert_not_called()
