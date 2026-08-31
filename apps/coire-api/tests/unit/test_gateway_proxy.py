from __future__ import annotations

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
