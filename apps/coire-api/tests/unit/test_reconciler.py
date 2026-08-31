"""Replication path invariants for the separated data fabric."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from coire_api.registry.reconciler import RegistryReconciler
from coire_core.net import DataFabricClient, FabricUnreachable


async def test_replication_client_never_falls_back_to_control() -> None:
    attempts: list[str] = []

    def fail(request: httpx.Request) -> httpx.Response:
        attempts.append(str(request.url))
        raise httpx.ConnectError("data link down", request=request)

    async with DataFabricClient(
        client=httpx.AsyncClient(transport=httpx.MockTransport(fail))
    ) as client:
        with pytest.raises(FabricUnreachable):
            await client.get("coire-edge-b", "/node/export/grant/manifest", port=9401)

    assert attempts == ["http://coire-edge-b.fabric:9401/node/export/grant/manifest"]


async def test_reconcile_wakeup_during_a_pass_is_not_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reconciler = RegistryReconciler(cast(Any, SimpleNamespace(registry_reconcile_interval_s=30.0)))
    passes = 0

    async def one_pass(_maker: object) -> None:
        nonlocal passes
        passes += 1
        if passes == 1:
            reconciler.request_reconcile("coire-edge-a")
        else:
            reconciler._stopping.set()
            reconciler._wake.set()

    class FakeEngine:
        async def dispose(self) -> None:
            return None

    monkeypatch.setattr(reconciler, "_pass", one_pass)
    monkeypatch.setattr(
        "coire_api.registry.reconciler.create_engine", lambda _settings: FakeEngine()
    )
    monkeypatch.setattr(
        "coire_api.registry.reconciler.async_sessionmaker", lambda *_args, **_kwargs: object()
    )

    await asyncio.wait_for(reconciler._run(), timeout=1.0)
    assert passes == 2
