"""Recovery readiness is independent of peer liveness and cannot mutate 019 ownership."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from coire_api.failover import cohort
from coire_core.settings import Settings


@pytest.mark.parametrize("fault", [None, "maintenance", "writer", "database", "scheduler"])
async def test_authoritative_cohort_requires_database_writer_and_services(
    monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    queries = AsyncMock()
    queries.scalar.return_value = "core" if fault == "maintenance" else None
    if fault == "database":
        queries.execute.side_effect = OSError("isolated database outage")

    @asynccontextmanager
    async def session() -> AsyncIterator[AsyncMock]:
        yield queries

    retained_owner = {"generation": 12, "identity": "executor", "released": False}
    preimage = dict(retained_owner)
    status = AsyncMock(
        return_value=SimpleNamespace(readiness="not_ready" if fault == "writer" else "ready")
    )
    monkeypatch.setattr(cohort, "session_scope", session)
    monkeypatch.setattr(cohort, "core_target_status", status)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(503 if fault == "scheduler" else 200)
        )
    ) as client:
        assert await cohort.authoritative_cohort_ready(Settings(upgrades_enabled=True), client) is (
            fault is None
        )
    assert retained_owner == preimage
    assert not queries.commit.called
    assert not queries.add.called
