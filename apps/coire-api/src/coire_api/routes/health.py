"""Health and readiness routes.

`/ready` is liveness only — process up, nothing external checked — because compose gates
dependency startup on it and a transient dependency fault must not make a healthy process look
dead. `/health` is the aggregate (FR-009, research R11).

Dependency probes run concurrently with individual timeouts so one slow dependency cannot make
`/health` itself slow (spec US2).
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Response, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api import __version__
from coire_api.auth import CurrentPrincipal
from coire_api.db import NodeRow
from coire_api.deps import SessionDep, SettingsDep
from coire_core.models.health import (
    HealthResponse,
    HealthStatus,
    ReadyResponse,
    ServiceHealth,
)
from coire_core.models.node import Reachability

router = APIRouter(tags=["health"])

PROBE_TIMEOUT_S = 2.0
SERVICE_NAME = "coire-api"

# Probing these is best-effort: their absence degrades, it does not fail (US2 scenario 3).
_HTTP_DEPENDENCIES: tuple[tuple[str, str], ...] = (
    ("mcp", "http://coire-mcp:8001/ready"),
    ("scheduler", "http://coire-scheduler:8002/ready"),
    ("otel-collector", "http://otel-collector:13133/"),
)


@router.get("/ready", response_model=ReadyResponse)
async def get_ready() -> ReadyResponse:
    """Liveness. Deliberately checks nothing external."""
    return ReadyResponse(service=SERVICE_NAME, version=__version__)


async def _probe_postgres(session: AsyncSession) -> ServiceHealth:
    started = time.perf_counter()
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_S):
            await session.execute(text("SELECT 1"))
        return ServiceHealth(
            name="postgres",
            healthy=True,
            checked_at=datetime.now(UTC),
            latency_ms=(time.perf_counter() - started) * 1000,
        )
    except Exception as exc:
        return ServiceHealth(
            name="postgres",
            healthy=False,
            detail=f"{type(exc).__name__}: {exc}",
            checked_at=datetime.now(UTC),
            latency_ms=(time.perf_counter() - started) * 1000,
        )


async def _probe_http(client: httpx.AsyncClient, name: str, url: str) -> ServiceHealth:
    started = time.perf_counter()
    try:
        resp = await client.get(url, timeout=PROBE_TIMEOUT_S)
        healthy = resp.status_code == 200
        return ServiceHealth(
            name=name,
            healthy=healthy,
            detail=None if healthy else f"HTTP {resp.status_code}",
            checked_at=datetime.now(UTC),
            latency_ms=(time.perf_counter() - started) * 1000,
        )
    except Exception as exc:
        return ServiceHealth(
            name=name,
            healthy=False,
            detail=f"{type(exc).__name__}: {exc}",
            checked_at=datetime.now(UTC),
            latency_ms=(time.perf_counter() - started) * 1000,
        )


async def _node_health(session: AsyncSession) -> list[ServiceHealth]:
    """Nodes as the prober last saw them; this route never probes a node itself."""
    rows = (await session.execute(select(NodeRow))).scalars().all()
    return [
        ServiceHealth(
            name=row.name,
            healthy=row.reachability is Reachability.HEALTHY,
            detail=None if row.reachability is Reachability.HEALTHY else row.reachability.value,
            checked_at=row.last_seen_at,
        )
        for row in rows
    ]


@router.get("/health", response_model=HealthResponse)
async def get_health(
    response: Response,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> HealthResponse:
    """Aggregate health.

    Postgres is the only critical dependency: without it the control plane has no system of
    record, so its failure is `unhealthy` (HTTP 503). Everything else degrades.
    """
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            _probe_postgres(session),
            *(_probe_http(client, name, url) for name, url in _HTTP_DEPENDENCIES),
        )

    postgres, *others = results
    services = list(results)

    try:
        nodes = await _node_health(session) if postgres.healthy else []
    except Exception:
        nodes = []

    if not postgres.healthy:
        overall = HealthStatus.UNHEALTHY
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif any(not s.healthy for s in others) or any(not n.healthy for n in nodes):
        overall = HealthStatus.DEGRADED
    else:
        overall = HealthStatus.HEALTHY

    return HealthResponse(
        status=overall,
        version=settings.service_version,
        services=services,
        nodes=nodes,
        generated_at=datetime.now(UTC),
    )
