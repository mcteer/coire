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
from typing import Any

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
FALLBACK_HEADER = "X-Coire-Path"
FALLBACK_VALUE = "fallback"


class MeshClient:
    """Talks to a peer over the mesh, falling back to egress with an explicit marker.

    The fallback is deliberately explicit: the peer refuses egress requests that do not carry
    the marker, so a misconfigured client cannot drift onto the slow path unnoticed.
    """

    def __init__(
        self,
        *,
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

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

        mesh_url = f"http://{host}{MESH_SUFFIX}{suffix}{path}"
        try:
            return await self._client.request(method, mesh_url, headers=base_headers, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            mesh_error = exc

        fallback_headers = {**base_headers, FALLBACK_HEADER: FALLBACK_VALUE}
        egress_url = f"http://{host}{EGRESS_SUFFIX}{suffix}{path}"
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
