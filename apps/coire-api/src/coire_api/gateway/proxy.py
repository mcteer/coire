"""Cancellation-aware, bounded transport to bare engine servers."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from coire_core.settings import Settings

_semaphores: dict[str, asyncio.Semaphore] = {}
_guard = asyncio.Lock()
_engine_client: httpx.AsyncClient | None = None


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
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=0.001)
    except TimeoutError as exc:
        raise EngineSaturatedError from exc
    try:
        yield
    finally:
        semaphore.release()


async def complete(
    engine_url: str, payload: dict[str, object], settings: Settings
) -> dict[str, object]:
    async with engine_slot(engine_url, settings):
        try:
            response = await _client().post(
                f"{engine_url}/v1/chat/completions",
                json=payload,
                timeout=settings.gateway_engine_request_timeout_s,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EngineProxyError(str(exc)) from exc
    if not isinstance(body, dict):
        raise EngineProxyError("engine returned a non-object response")
    return body


async def stream(
    engine_url: str, payload: dict[str, object], settings: Settings
) -> AsyncIterator[bytes]:
    async with engine_slot(engine_url, settings):
        timeout = httpx.Timeout(settings.gateway_engine_request_timeout_s, read=None)
        done = False
        try:
            async with _client().stream(
                "POST",
                f"{engine_url}/v1/chat/completions",
                json=payload,
                timeout=timeout,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line:
                        if line.strip() == "data: [DONE]":
                            done = True
                        yield f"{line}\n\n".encode()
        except httpx.HTTPError as exc:
            if done:
                return
            error = json.dumps(
                {"error": {"message": "engine stream failed", "type": "engine_error"}}
            )
            yield f"data: {error}\n\n".encode()
            raise EngineProxyError(str(exc)) from exc
