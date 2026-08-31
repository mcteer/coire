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

from coire_core.net import (
    FALLBACK_HEADER,
    FALLBACK_VALUE,
    ControlClient,
    DataFabricClient,
    FabricUnreachable,
    MeshClient,
)


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


class TestNoFallbackClient:
    """Replication may not cross the egress path (spec FR-007, SC-004)."""

    async def test_mesh_failure_raises_instead_of_trying_egress(self) -> None:
        import httpx
        import pytest

        from coire_core.net import MeshClient, MeshUnreachable

        attempted: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempted.append(str(request.url))
            raise httpx.ConnectError("down", request=request)

        transport = httpx.MockTransport(handler)
        async with MeshClient(
            client=httpx.AsyncClient(transport=transport), fallback=False
        ) as client:
            with pytest.raises(MeshUnreachable):
                await client.get("coire-edge-b", "/node/health")

        assert len(attempted) == 1
        assert ".mesh" in attempted[0]
        assert ".local" not in attempted[0]

    async def test_default_client_still_falls_back(self) -> None:
        import httpx

        from coire_core.net import MeshClient

        attempted: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempted.append(str(request.url))
            if ".mesh" in str(request.url):
                raise httpx.ConnectError("down", request=request)
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(handler)
        async with MeshClient(client=httpx.AsyncClient(transport=transport)) as client:
            resp = await client.get("coire-edge-b", "/node/health")

        assert resp.status_code == 200
        assert len(attempted) == 2 and ".local" in attempted[1]


class TestHostSuffixing:
    """A caller passes a bare node name; the suffix is added here (ADR-0002)."""

    def test_a_bare_name_gets_the_suffix(self) -> None:
        from coire_core.net import MESH_SUFFIX, _host_with

        assert _host_with("coire-edge-a", MESH_SUFFIX) == "coire-edge-a.mesh"

    def test_a_literal_address_is_left_alone(self) -> None:
        """Appending a DNS suffix to an address produces a name that cannot resolve."""
        from coire_core.net import MESH_SUFFIX, _host_with

        assert _host_with("192.168.100.11", MESH_SUFFIX) == "192.168.100.11"
        assert _host_with("127.0.0.1", MESH_SUFFIX) == "127.0.0.1"

    def test_an_already_suffixed_name_is_not_doubled(self) -> None:
        from coire_core.net import EGRESS_SUFFIX, MESH_SUFFIX, _host_with

        assert _host_with("coire-edge-a.mesh", MESH_SUFFIX) == "coire-edge-a.mesh"
        assert _host_with("coire-edge-a.local", EGRESS_SUFFIX) == "coire-edge-a.local"


class TestSeparatedFabricClients:
    async def test_control_uses_unifi_name_only(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200)

        async with ControlClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        ) as client:
            await client.get("coire-edge-a", "/node/health", port=9400)

        assert seen == ["http://coire-edge-a:9400/node/health"]

    async def test_data_uses_fabric_name_only(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200)

        async with DataFabricClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        ) as client:
            await client.get("coire-edge-b", "/models/export", port=9401)

        assert seen == ["http://coire-edge-b.fabric:9401/models/export"]

    @pytest.mark.parametrize("client_type", [ControlClient, DataFabricClient])
    async def test_connect_failure_never_crosses_fabrics(self, client_type: type) -> None:
        attempted: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempted.append(str(request.url))
            raise httpx.ConnectError("down", request=request)

        async with client_type(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        ) as client:
            with pytest.raises(FabricUnreachable):
                await client.get("coire-edge-a", "/probe")

        assert len(attempted) == 1
