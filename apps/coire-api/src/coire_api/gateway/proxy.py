"""Cancellation-aware, bounded transport to bare engine servers."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from time import perf_counter
from urllib.parse import urlparse

import httpx
from opentelemetry.propagate import inject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import (
    EngineProcessRow,
    InstanceMemberRow,
    MemoryReservationRow,
    ModelInstanceRow,
    ShardGroupRow,
    session_scope,
)
from coire_api.gateway.telemetry import lease_loss_counter, queue_duration_ms, tracer
from coire_api.placement.legacy import (
    ensure_legacy_model_hold,
    ensure_legacy_model_hold_locked,
)
from coire_api.placement.service import (
    acquire_lease,
    lock_nodes_for_admission,
    refresh_lease,
    release_lease,
)
from coire_core.errors import ChatModelUnavailable
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.settings import Settings

_semaphores: dict[str, asyncio.Semaphore] = {}
_guard = asyncio.Lock()
_engine_client: httpx.AsyncClient | None = None
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StreamTiming:
    request_started_at: float = field(default_factory=perf_counter)
    upstream_started_at: float | None = None
    first_chunk_at: float | None = None


def init_engine_client() -> None:
    """Create the process-wide engine connection pool during application startup."""

    global _engine_client
    if _engine_client is None:
        _engine_client = httpx.AsyncClient(
            # Retire idle sockets before the node's five-second keepalive closes
            # them near a fixed workload arrival. Do not replay failed streams.
            limits=httpx.Limits(
                max_connections=32, max_keepalive_connections=8, keepalive_expiry=1.0
            )
        )


async def close_engine_client() -> None:
    global _engine_client
    client, _engine_client = _engine_client, None
    if client is not None:
        await client.aclose()


def _client() -> httpx.AsyncClient:
    init_engine_client()
    assert _engine_client is not None
    return _engine_client


def _node_headers(engine_url: str, settings: Settings) -> dict[str, str]:
    parsed = urlparse(engine_url)
    if not parsed.path.startswith(("/node/engines/", "/node/shard-groups/")):
        return {}
    node = (parsed.hostname or "").split(".", 1)[0]
    token = settings.node_token_map.get(node, "")
    if not token:
        return {}
    headers = {"Authorization": f"Bearer {token}"}
    inject(headers)
    return headers


async def _semaphore(engine_url: str, limit: int) -> asyncio.Semaphore:
    async with _guard:
        semaphore = _semaphores.get(engine_url)
        if semaphore is None:
            semaphore = asyncio.Semaphore(limit)
            _semaphores[engine_url] = semaphore
        return semaphore


class EngineSaturatedError(Exception):
    pass


class EngineProxyError(Exception):
    pass


def _stream_failure_event(retry_after_s: int = 1) -> bytes:
    """Carry retry guidance after an SSE response has committed its HTTP headers."""
    error = json.dumps(
        {
            "error": {
                "message": "engine stream failed",
                "type": "engine_error",
                "coire_retry_after": retry_after_s,
            }
        }
    )
    return f"retry: {retry_after_s * 1000}\ndata: {error}\n\n".encode()


@asynccontextmanager
async def engine_slot(engine_url: str, settings: Settings) -> AsyncIterator[None]:
    semaphore = await _semaphore(engine_url, settings.gateway_max_inflight_per_engine)
    queued_at = perf_counter()
    with tracer.start_as_current_span("coire.gateway.queue") as span:
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=0.001)
        except TimeoutError as exc:
            span.set_attribute("coire.gateway.queue.outcome", "saturated")
            queue_duration_ms.record((perf_counter() - queued_at) * 1000, {"outcome": "saturated"})
            raise EngineSaturatedError from exc
        span.set_attribute("coire.gateway.queue.outcome", "admitted")
    queue_duration_ms.record((perf_counter() - queued_at) * 1000, {"outcome": "admitted"})
    try:
        yield
    finally:
        semaphore.release()


async def _ensure_legacy_engine_hold(
    session: AsyncSession, engine: EngineProcessRow, holder_id: str
) -> None:
    """Fence a live legacy engine in the shared ledger before serving inference."""
    await ensure_legacy_model_hold(
        session, engine.node_id, uuid.UUID(holder_id), engine.estimate_bytes
    )


async def _ensure_legacy_engine_hold_locked(
    session: AsyncSession, engine: EngineProcessRow, holder_id: str
) -> None:
    from coire_api.placement.service import training_allows_work

    if not await training_allows_work(session, [engine.node_id]):
        raise ChatModelUnavailable()
    await ensure_legacy_model_hold_locked(
        session, engine.node_id, uuid.UUID(holder_id), engine.estimate_bytes
    )


@asynccontextmanager
async def request_lease(engine_url: str, settings: Settings) -> AsyncIterator[None]:
    """Protect a resolved model from TTL/eviction for the full upstream request lifetime."""
    parsed = urlparse(engine_url)
    segments = parsed.path.split("/")
    try:
        target_id = uuid.UUID(segments[3])
    except (ValueError, IndexError):
        yield
        return
    lease_ids: list[uuid.UUID] = []
    instance_id: uuid.UUID | None = None
    async with session_scope() as session:
        from coire_api.training.gateway_measurements import authorize_measurement_lease

        await authorize_measurement_lease(session, engine_url)
        from coire_api.evaluation.gateway_measurements import authorize_probe_lease

        await authorize_probe_lease(session, engine_url)
        if len(segments) > 2 and segments[2] == "shard-groups":
            group = await session.get(ShardGroupRow, target_id)
            if group is None:
                raise ChatModelUnavailable()
            instance_id = group.instance_id
            members = (
                list(
                    (
                        await session.execute(
                            select(InstanceMemberRow).where(
                                InstanceMemberRow.instance_id == instance_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if instance_id is not None
                else []
            )
            if not members:
                raise ChatModelUnavailable()
            # Acquire the entire group in canonical order before any individual
            # request lease; opposite rank orders must not deadlock admissions.
            await lock_nodes_for_admission(session, [member.node_id for member in members])
            for member in members:
                if member.reservation_id is None:
                    raise ChatModelUnavailable()
                lease = await acquire_lease(
                    session,
                    member.reservation_id,
                    str(uuid.uuid4()),
                    ttl_seconds=settings.placement_lease_ttl_s,
                )
                lease_ids.append(lease.id)
        else:
            engine = await session.get(EngineProcessRow, target_id)
            if engine is not None:
                instance_id = engine.instance_id
        if len(segments) <= 2 or segments[2] != "shard-groups":
            engine = await session.get(EngineProcessRow, target_id)
        else:
            engine = None
        if engine is not None and engine.model_id is not None:
            holder_id = str(engine.instance_id or engine.model_id)
            if engine.instance_id is None:
                await _ensure_legacy_engine_hold_locked(session, engine, holder_id)
            reservation = await session.scalar(
                select(MemoryReservationRow).where(
                    MemoryReservationRow.node_id == engine.node_id,
                    MemoryReservationRow.holder_type == ReservationHolder.MODEL,
                    MemoryReservationRow.holder_id == holder_id,
                    MemoryReservationRow.state == MemoryReservationState.HELD,
                )
            )
            if reservation is None:
                raise ChatModelUnavailable()
            lease = await acquire_lease(
                session,
                reservation.id,
                str(uuid.uuid4()),
                ttl_seconds=settings.placement_lease_ttl_s,
            )
            lease_ids.append(lease.id)
        elif engine is None and len(segments) > 2 and segments[2] != "shard-groups":
            raise ChatModelUnavailable()
        if instance_id is not None and lease_ids:
            instance = await session.get(ModelInstanceRow, instance_id)
            if instance is not None:
                instance.in_flight += 1
    try:
        stop_refresh = asyncio.Event()
        request_task = asyncio.current_task()

        async def keep_fresh() -> None:
            interval = max(0.1, settings.placement_lease_ttl_s / 2)
            while not stop_refresh.is_set():
                try:
                    await asyncio.wait_for(stop_refresh.wait(), timeout=interval)
                    return
                except TimeoutError:
                    with tracer.start_as_current_span("coire.gateway.lease_renewal"):
                        try:
                            async with session_scope() as session:
                                await authorize_measurement_lease(session, engine_url)
                                await authorize_probe_lease(session, engine_url)
                                refreshed = [
                                    await refresh_lease(
                                        session,
                                        lease_id,
                                        ttl_seconds=settings.placement_lease_ttl_s,
                                    )
                                    for lease_id in lease_ids
                                ]
                        except Exception:
                            lease_loss_counter.add(1, {"reason": "unavailable"})
                            logger.error(
                                "gateway memory lease renewal unavailable",
                                extra={"instance_id": str(instance_id)},
                            )
                            if request_task is not None:
                                request_task.cancel()
                            return
                        if not all(refreshed):
                            lease_loss_counter.add(1, {"reason": "expired"})
                            logger.error(
                                "gateway memory lease expired during inference",
                                extra={"instance_id": str(instance_id)},
                            )
                            if request_task is not None:
                                request_task.cancel()
                            return

        refresher = asyncio.create_task(keep_fresh()) if lease_ids else None
        yield
    finally:
        stop_refresh.set()
        if refresher is not None:
            await refresher
        if lease_ids:
            async with session_scope() as session:
                for lease_id in lease_ids:
                    await release_lease(session, lease_id)
                if instance_id is not None:
                    instance = await session.get(ModelInstanceRow, instance_id)
                    if instance is not None:
                        instance.in_flight = max(0, instance.in_flight - 1)


async def complete(
    engine_url: str, payload: dict[str, object], settings: Settings
) -> dict[str, object]:
    with tracer.start_as_current_span("coire.gateway.generation") as span:
        span.set_attribute("coire.gateway.streaming", False)
        async with engine_slot(engine_url, settings), request_lease(engine_url, settings):
            try:
                with tracer.start_as_current_span("coire.gateway.upstream"):
                    response = await _client().post(
                        f"{engine_url}/v1/chat/completions",
                        json=payload,
                        headers=_node_headers(engine_url, settings),
                        timeout=settings.gateway_engine_request_timeout_s,
                    )
                    if response.status_code == 422:
                        try:
                            errors = response.json().get("detail", [])
                        except (ValueError, AttributeError):
                            errors = []
                        safe_errors = [
                            {
                                "loc": item.get("loc", []),
                                "type": item.get("type", "unknown"),
                                "msg": item.get("msg", "validation failed"),
                            }
                            for item in errors
                            if isinstance(item, dict)
                        ]
                        logger.warning(
                            "node engine proxy rejected gateway contract status=%d errors=%s",
                            response.status_code,
                            json.dumps(safe_errors, separators=(",", ":")),
                        )
                    response.raise_for_status()
                    body = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                span.record_exception(exc)
                raise EngineProxyError(str(exc)) from exc
    if not isinstance(body, dict):
        raise EngineProxyError("engine returned a non-object response")
    return body


async def stream(
    engine_url: str,
    payload: dict[str, object],
    settings: Settings,
    timing: StreamTiming | None = None,
) -> AsyncIterator[bytes]:
    with tracer.start_as_current_span("coire.gateway.generation") as span:
        span.set_attribute("coire.gateway.streaming", True)
        async with engine_slot(engine_url, settings), request_lease(engine_url, settings):
            timeout = httpx.Timeout(settings.gateway_engine_request_timeout_s, read=None)
            done = False
            try:
                if timing is not None:
                    timing.upstream_started_at = perf_counter()
                with tracer.start_as_current_span("coire.gateway.upstream"):
                    async with _client().stream(
                        "POST",
                        f"{engine_url}/v1/chat/completions",
                        json=payload,
                        headers=_node_headers(engine_url, settings),
                        timeout=timeout,
                    ) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if line:
                                if timing is not None and timing.first_chunk_at is None:
                                    timing.first_chunk_at = perf_counter()
                                if line.strip() == "data: [DONE]":
                                    done = True
                                yield f"{line}\n\n".encode()
                        if not done:
                            yield _stream_failure_event()
                            raise EngineProxyError("engine stream ended without a terminator")
            except httpx.HTTPError as exc:
                if done:
                    return
                span.record_exception(exc)
                yield _stream_failure_event()
                raise EngineProxyError(str(exc)) from exc
