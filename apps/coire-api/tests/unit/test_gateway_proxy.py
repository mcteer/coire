from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast

import httpx
import pytest

from coire_api.gateway import proxy
from coire_core.settings import Settings


@pytest.mark.asyncio
async def test_engine_client_is_reused_and_closed() -> None:
    await proxy.close_engine_client()
    proxy.init_engine_client()
    first = proxy._client()
    assert proxy._client() is first

    await proxy.close_engine_client()
    assert first.is_closed


@pytest.mark.asyncio
async def test_stream_records_upstream_first_chunk_timing() -> None:
    async def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"data: {}\n\ndata: [DONE]\n\n")

    await proxy.close_engine_client()
    proxy._engine_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    timing = proxy.StreamTiming()
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]

    chunks = [chunk async for chunk in proxy.stream("http://engine", {}, settings, timing)]

    assert chunks
    assert timing.upstream_started_at is not None
    assert timing.first_chunk_at is not None
    assert timing.request_started_at <= timing.upstream_started_at <= timing.first_chunk_at
    await proxy.close_engine_client()


@pytest.mark.asyncio
async def test_closing_gateway_stream_promptly_closes_upstream() -> None:
    closed = False

    class EndlessStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'data: {"choices":[{"delta":{"content":"one"}}]}\n\n'
            raise AssertionError("gateway consumed upstream after client close")

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    async def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=EndlessStream())

    await proxy.close_engine_client()
    proxy._engine_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    source = cast(AsyncGenerator[bytes], proxy.stream("http://engine", {}, settings))
    assert await anext(source)
    await source.aclose()
    assert closed
    await proxy.close_engine_client()


@pytest.mark.asyncio
async def test_node_proxy_request_acquires_and_releases_memory_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    lease_id = uuid.uuid4()
    calls: list[tuple[str, object]] = []

    class Session:
        async def get(self, _model: object, _id: object) -> object:
            return SimpleNamespace(model_id=uuid.uuid4(), instance_id=None, node_id=uuid.uuid4())

        async def scalar(self, _query: object) -> object:
            return SimpleNamespace(id=reservation_id)

    @asynccontextmanager
    async def sessions() -> AsyncIterator[Session]:
        yield Session()

    async def acquire(
        _session: object, reservation: uuid.UUID, request: str, *, ttl_seconds: float
    ) -> object:
        calls.append(("acquire", reservation))
        assert request
        assert ttl_seconds == 60
        return SimpleNamespace(id=lease_id)

    async def release(_session: object, lease: uuid.UUID) -> None:
        calls.append(("release", lease))

    monkeypatch.setattr(proxy, "session_scope", sessions)
    monkeypatch.setattr(proxy, "acquire_lease", acquire)
    monkeypatch.setattr(proxy, "release_lease", release)
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]

    async with proxy.request_lease(
        f"http://coire-edge-a.lab:9400/node/engines/{engine_id}/proxy", settings
    ):
        assert calls == [("acquire", reservation_id)]
    assert calls == [("acquire", reservation_id), ("release", lease_id)]
