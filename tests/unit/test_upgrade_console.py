"""Upgrade capability is freshly authorized per request and never shared through the cache."""

import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coire_api.auth import Principal
from coire_api.console import service
from coire_api.upgrades import authorization
from coire_core.errors import UpgradeForbidden
from coire_core.models.console import ConsoleCapabilities, ConsoleSnapshot
from coire_core.models.instance import ClusterState
from coire_core.settings import Settings


@pytest.mark.parametrize("enabled", [True, False])
async def test_console_cached_snapshot_cannot_cache_upgrade_authority(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    cache = service.SnapshotCache()
    now = datetime.now(UTC)
    cache.snapshot = ConsoleSnapshot(
        observed_at=now,
        cursor="1",
        capabilities=ConsoleCapabilities(),
        cluster=ClusterState(observed_at=now, nodes=[], instances=[]),
        ledgers=[],
    )
    cache.expires_at = time.monotonic() + 60
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(console_snapshot_cache=cache))
    )
    authorize = AsyncMock(side_effect=[None, UpgradeForbidden()])
    monkeypatch.setattr(authorization, "authorize", authorize)

    @asynccontextmanager
    async def sessions():
        yield AsyncMock()

    monkeypatch.setattr(service, "session_scope", sessions)
    settings = Settings(upgrades_enabled=enabled)
    first = await service.cached_snapshot(request, Principal(), settings)
    second = await service.cached_snapshot(request, Principal(), settings)
    assert first.capabilities.upgrades is enabled
    assert second.capabilities.upgrades is False
    assert cache.snapshot.capabilities.upgrades is False
    assert authorize.await_count == (2 if enabled else 0)
