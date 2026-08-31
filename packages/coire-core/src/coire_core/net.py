"""Mesh-first HTTP client.

Platform traffic prefers the Thunderbolt mesh and falls back to the egress interface only when
the mesh path to a peer is unreachable (spec FR-013a/b). Every fallback is counted and logged
at WARNING so sustained slow-path operation is never silent (FR-013c); the alert rule on the
counter belongs to feature 009 (ADR-0003).

Measured on this cluster: mesh 12.0-12.6 Gb/s at 0.85-1.37 ms; egress 0.4 Gb/s at 23-29 ms.
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Any, Self

import httpx
from opentelemetry import metrics

logger = logging.getLogger(__name__)

_meter = metrics.get_meter("coire.net")
fallback_counter = _meter.create_counter(
    "coire_fallback_requests_total",
    unit="1",
    description="Requests that used the egress interface because the mesh path was unreachable.",
)

MESH_SUFFIX = ".mesh"
EGRESS_SUFFIX = ".local"
DATA_SUFFIX = ".fabric"


def _host_with(host: str, suffix: str) -> str:
    """Append the mesh or egress suffix, unless the host already carries one or is a literal
    address.

    Callers pass a bare node name (`coire-edge-a`) and the suffix is added here so nothing
    hard-codes an address (ADR-0002). But appending a DNS suffix to `192.168.100.11` produces
    a name that cannot resolve, and appending `.mesh` to `coire-edge-a.mesh` produces one that
    is simply wrong — so neither is done.
    """
    import ipaddress

    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return host
    if host.endswith((MESH_SUFFIX, EGRESS_SUFFIX, DATA_SUFFIX)):
        return host
    return f"{host}{suffix}"


FALLBACK_HEADER = "X-Coire-Path"
FALLBACK_VALUE = "fallback"


class MeshUnreachable(RuntimeError):
    """The mesh path to a peer failed and falling back was not permitted.

    Raised only by a client constructed with `fallback=False` — model replication, which spec
    FR-007 requires to stay on the mesh. Failing is the correct outcome there: a 200 GB copy
    crossing Wi-Fi at 1/30th the speed would be worse than a clear refusal, and SC-004 asserts
    it never happens.
    """


class MeshClient:
    """Talks to a peer over the mesh, falling back to egress with an explicit marker.

    The fallback is deliberately explicit: the peer refuses egress requests that do not carry
    the marker, so a misconfigured client cannot drift onto the slow path unnoticed.

    `fallback=False` disables it entirely and raises `MeshUnreachable` instead (spec FR-007).
    """

    def __init__(
        self,
        *,
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
        fallback: bool = True,
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None
        self._fallback = fallback

    async def __aenter__(self) -> MeshClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def request(
        self,
        method: str,
        host: str,
        path: str,
        *,
        port: int | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Issue `method path` to `host`, preferring the mesh.

        `host` is a bare node name (e.g. `coire-edge-a`); the suffix is added here so no caller
        ever hard-codes an address (ADR-0002).
        """
        suffix = f":{port}" if port else ""
        base_headers = dict(headers or {})

        mesh_url = f"http://{_host_with(host, MESH_SUFFIX)}{suffix}{path}"
        try:
            return await self._client.request(method, mesh_url, headers=base_headers, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            mesh_error = exc

        if not self._fallback:
            raise MeshUnreachable(
                f"mesh path to {host} unreachable ({mesh_error}); this client may not use the "
                "egress path (FR-007)"
            ) from mesh_error

        fallback_headers = {**base_headers, FALLBACK_HEADER: FALLBACK_VALUE}
        egress_url = f"http://{_host_with(host, EGRESS_SUFFIX)}{suffix}{path}"
        fallback_counter.add(1, {"peer": host})
        logger.warning(
            "mesh path to %s unreachable (%s); falling back to egress %s — "
            "this path is ~30x slower and should not be the steady state",
            host,
            mesh_error,
            egress_url,
        )
        return await self._client.request(method, egress_url, headers=fallback_headers, **kwargs)

    async def get(self, host: str, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", host, path, **kwargs)

    async def post(self, host: str, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", host, path, **kwargs)

    def stream(
        self,
        method: str,
        host: str,
        path: str,
        *,
        port: int | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Streaming request context manager, mesh only.

        Deliberately does not fall back: the one streaming caller is the replication import,
        and re-issuing a partially consumed multi-gigabyte transfer on the slow path is not a
        recovery, it is a much longer failure. Range requests resume it instead.
        """
        suffix = f":{port}" if port else ""
        url = f"http://{_host_with(host, MESH_SUFFIX)}{suffix}{path}"
        return self._client.stream(method, url, headers=dict(headers or {}), **kwargs)


class FabricUnreachable(RuntimeError):
    """A fixed-purpose network path could not reach its peer."""


class _FixedPathClient:
    def __init__(
        self,
        suffix: str,
        path_name: str,
        *,
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._suffix = suffix
        self._path_name = path_name
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _url(self, host: str, path: str, port: int | None) -> str:
        port_part = f":{port}" if port else ""
        return f"http://{_host_with(host, self._suffix)}{port_part}{path}"

    async def request(
        self,
        method: str,
        host: str,
        path: str,
        *,
        port: int | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        url = self._url(host, path, port)
        try:
            return await self._client.request(method, url, headers=dict(headers or {}), **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise FabricUnreachable(
                f"{self._path_name} path to {host} unreachable; cross-fabric fallback is forbidden"
            ) from exc

    async def get(self, host: str, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", host, path, **kwargs)

    async def post(self, host: str, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", host, path, **kwargs)

    def stream(self, method: str, host: str, path: str, **kwargs: Any) -> Any:
        port = kwargs.pop("port", None)
        headers = kwargs.pop("headers", None)
        return self._client.stream(
            method, self._url(host, path, port), headers=dict(headers or {}), **kwargs
        )


class ControlClient(_FixedPathClient):
    """Control traffic over UniFi DNS, with no data-fabric fallback."""

    def __init__(self, *, timeout: float = 5.0, client: httpx.AsyncClient | None = None) -> None:
        super().__init__("", "control", timeout=timeout, client=client)


class DataFabricClient(_FixedPathClient):
    """Replication/data traffic over the Studio-only ``.fabric`` link."""

    def __init__(self, *, timeout: float = 5.0, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(DATA_SUFFIX, "data", timeout=timeout, client=client)
