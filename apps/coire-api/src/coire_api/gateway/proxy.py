"""Cancellation-aware, bounded transport to bare engine servers."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from time import perf_counter
from urllib.parse import urlparse

import httpx
from opentelemetry.propagate import inject

from coire_api.gateway.telemetry import queue_duration_ms, tracer
from coire_core.settings import Settings

_semaphores: dict[str, asyncio.Semaphore] = {}
_guard = asyncio.Lock()
_engine_client: httpx.AsyncClient | None = None


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
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=8)
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
    if not parsed.path.startswith("/node/engines/"):
        return {}
    node = (parsed.hostname or "").split(".", 1)[0]
    token = settings.node_token_map.get(node, "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
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


@asynccontextmanager
async def engine_slot(engine_url: str, settings: Settings) -> AsyncIterator[None]:
    semaphore = await _semaphore(engine_url, settings.gateway_max_inflight_per_engine)
    queued_at = perf_counter()
    with tracer.start_as_current_span("coire.scheduler.admission_wait") as span:
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


async def complete(
    engine_url: str, payload: dict[str, object], settings: Settings
) -> dict[str, object]:
    with tracer.start_as_current_span("coire.gateway.generation") as span:
        span.set_attribute("coire.gateway.streaming", False)
        async with engine_slot(engine_url, settings):
            try:
                with tracer.start_as_current_span("coire.gateway.upstream"):
                    response = await _client().post(
                        f"{engine_url}/v1/chat/completions",
                        json=payload,
                        headers=_node_headers(engine_url, settings),
                        timeout=settings.gateway_engine_request_timeout_s,
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
        async with engine_slot(engine_url, settings):
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
                            error = json.dumps(
                                {
                                    "error": {
                                        "message": "engine stream failed",
                                        "type": "engine_error",
                                    }
                                }
                            )
                            yield f"data: {error}\n\n".encode()
                            raise EngineProxyError("engine stream ended without a terminator")
            except httpx.HTTPError as exc:
                if done:
                    return
                span.record_exception(exc)
                error = json.dumps(
                    {"error": {"message": "engine stream failed", "type": "engine_error"}}
                )
                yield f"data: {error}\n\n".encode()
                raise EngineProxyError(str(exc)) from exc
