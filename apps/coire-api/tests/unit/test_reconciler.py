"""Replication path invariants for the separated data fabric."""

from __future__ import annotations

import httpx
import pytest

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
