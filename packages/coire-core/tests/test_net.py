"""Unit tests for the mesh-first client (T011).

The properties under test are the ones that keep the egress path from becoming the silent
steady state: the mesh is tried first, fallback happens only on a connect failure, and every
fallback carries the explicit marker.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import httpx
import pytest

from coire_core.net import FALLBACK_HEADER, FALLBACK_VALUE, MeshClient


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> MeshClient:
    return MeshClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_prefers_mesh_and_sends_no_marker() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    async with _client(handler) as mc:
        resp = await mc.get("coire-edge-a", "/node/health", port=9400)

    assert resp.status_code == 200
    assert str(seen[0].url) == "http://coire-edge-a.mesh:9400/node/health"
    assert FALLBACK_HEADER not in seen[0].headers


async def test_falls_back_to_egress_on_connect_error() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if ".mesh" in request.url.host:
            raise httpx.ConnectError("no route to mesh", request=request)
        return httpx.Response(200, json={"ok": True})

    async with _client(handler) as mc:
        resp = await mc.get("coire-edge-a", "/node/health", port=9400)

    assert resp.status_code == 200
    assert [r.url.host for r in seen] == ["coire-edge-a.mesh", "coire-edge-a.local"]
    assert seen[1].headers[FALLBACK_HEADER] == FALLBACK_VALUE


async def test_fallback_is_logged_at_warning(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if ".mesh" in request.url.host:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200)

    with caplog.at_level(logging.WARNING, logger="coire_core.net"):
        async with _client(handler) as mc:
            await mc.get("coire-edge-a", "/node/health")

    assert any("falling back to egress" in r.getMessage() for r in caplog.records)


async def test_http_error_does_not_trigger_fallback() -> None:
    """A 500 from the mesh is a peer problem, not a path problem — do not retry on egress."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500)

    async with _client(handler) as mc:
        resp = await mc.get("coire-edge-a", "/node/health")

    assert resp.status_code == 500
    assert len(seen) == 1


async def test_egress_failure_propagates() -> None:
    """If both paths are down the caller must see the error, not a silent success."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(httpx.ConnectError):
        async with _client(handler) as mc:
            await mc.get("coire-edge-a", "/node/health")


async def test_caller_headers_are_preserved_on_both_paths() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if ".mesh" in request.url.host:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200)

    async with _client(handler) as mc:
        await mc.get("coire-edge-a", "/node/health", headers={"Authorization": "Bearer t"})

    assert seen[0].headers["Authorization"] == "Bearer t"
    assert seen[1].headers["Authorization"] == "Bearer t"
