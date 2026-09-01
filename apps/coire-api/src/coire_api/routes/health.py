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
from opentelemetry import metrics
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api import __version__
from coire_api.auth import CurrentPrincipal
from coire_api.db import NodeRow
from coire_api.deps import SessionDep, SettingsDep
from coire_api.health_evaluator import is_fresh
from coire_api.nodes_client import NodeClient, NodeError
from coire_core.models.health import (
    HealthResponse,
    HealthStatus,
    NodeHealth,
    ReadyResponse,
    ServiceHealth,
)
from coire_core.models.link import LinkState, StudioDataLinkStatus
from coire_core.models.node import NodeStatus, Reachability
from coire_core.settings import Settings

router = APIRouter(tags=["health"])
_meter = metrics.get_meter("coire.api.health")
_health_verdict = _meter.create_gauge("coire_node_health_verdict")
_heartbeat_age = _meter.create_gauge("coire_node_heartbeat_age_seconds", unit="s")
_clock_skew = _meter.create_gauge("coire_node_clock_skew_seconds", unit="s")
_tunnel_up = _meter.create_gauge("coire_tunnel_up")

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


async def _node_health(session: AsyncSession, settings: Settings) -> list[NodeHealth]:
    """Nodes as the prober last saw them; this route never probes a node itself."""
    rows = (await session.execute(select(NodeRow))).scalars().all()
    now = datetime.now(UTC)
    result: list[NodeHealth] = []
    for row in rows:
        fresh = is_fresh(row.last_observed_at, now, settings)
        verdict = row.reachability if fresh else Reachability.UNKNOWN
        observation = (
            NodeStatus.model_validate(row.last_observation) if row.last_observation else None
        )
        skew = (
            (observation.sampled_at - row.last_observed_at).total_seconds()
            if observation is not None and row.last_observed_at is not None
            else None
        )
        result.append(
            NodeHealth(
                name=row.name,
                verdict=verdict,
                reason=None if verdict is Reachability.HEALTHY else verdict.value,
                fresh=fresh,
                last_observed_at=row.last_observed_at or row.last_seen_at,
                last_success_at=row.last_seen_at,
                seconds_since_heartbeat=max(0.0, (now - row.last_seen_at).total_seconds()),
                heartbeat_latency_ms=row.heartbeat_latency_ms,
                clock_skew_seconds=skew,
                process_state_verified=fresh and verdict is not Reachability.UNREACHABLE,
                observation=observation,
            )
        )
        _health_verdict.set(1, {"node": row.name, "verdict": verdict.value})
        _heartbeat_age.set(result[-1].seconds_since_heartbeat, {"node": row.name})
        if skew is not None:
            _clock_skew.set(skew, {"node": row.name})
    return result


async def _link_health(settings: Settings) -> list[StudioDataLinkStatus]:
    """Read the canonical Studio link without making its failure break `/health`."""
    try:
        async with NodeClient(settings, timeout=PROBE_TIMEOUT_S) as client:
            link = await client.data_link_status("coire-edge-a")
            now = datetime.now(UTC)
            if link.measured_at is None or not is_fresh(link.measured_at, now, settings):
                link = link.model_copy(
                    update={"ip_state": LinkState.UNKNOWN, "reason": "stale link observation"}
                )
            return [link]
    except (NodeError, httpx.HTTPError, ValueError):
        return [
            StudioDataLinkStatus(
                node_a="coire-edge-a",
                node_b="coire-edge-b",
                ip_state=LinkState.UNKNOWN,
                measured_at=datetime.now(UTC),
                reason="link observation unavailable",
            )
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
        results = list(
            await asyncio.gather(
                _probe_postgres(session),
                *(_probe_http(client, name, url) for name, url in _HTTP_DEPENDENCIES),
            )
        )
        if settings.tunnel_health_url:
            tunnel = await _probe_http(client, "tunnel", settings.tunnel_health_url)
            results.append(tunnel)
            _tunnel_up.set(1 if tunnel.healthy else 0, {"tunnel": "primary"})

    postgres, *others = results
    services = list(results)

    try:
        nodes, links = (
            await asyncio.gather(_node_health(session, settings), _link_health(settings))
            if postgres.healthy
            else ([], [])
        )
    except Exception:
        nodes = []
        links = []

    if not postgres.healthy:
        overall = HealthStatus.UNHEALTHY
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif (
        any(not s.healthy for s in others)
        or any(n.verdict is not Reachability.HEALTHY for n in nodes)
        or any(link.ip_state is not LinkState.UP for link in links)
    ):
        overall = HealthStatus.DEGRADED
    else:
        overall = HealthStatus.HEALTHY

    return HealthResponse(
        status=overall,
        version=settings.service_version,
        services=services,
        nodes=nodes,
        links=links,
        generated_at=datetime.now(UTC),
    )
