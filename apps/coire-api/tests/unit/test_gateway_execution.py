"""Shared stream execution keeps accounting and public transport stable."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from time import perf_counter
from types import SimpleNamespace

import pytest

from coire_api.auth import ANONYMOUS
from coire_api.gateway.execution import rewrite_openai_model, track_stream
from coire_api.gateway.proxy import StreamTiming
from coire_api.gateway.usage import UsageTracker
from coire_core.models.gateway import GatewayProtocol, UsageOutcome


async def test_fragmented_sse_usage_is_counted_once(monkeypatch: pytest.MonkeyPatch) -> None:
    saved: list[dict[str, object]] = []

    async def persist(**kwargs: object) -> None:
        saved.append(kwargs)

    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", persist)

    async def source() -> AsyncIterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"hi"}}],"usa'
        yield b'ge":{"prompt_tokens":8,"completion_tokens":3}}\r'
        yield b"\n\r\ndata: [DONE]\r\n\r\n"

    usage = UsageTracker(ANONYMOUS, str(uuid.uuid4()), GatewayProtocol.OPENAI)
    raw = b"".join([chunk async for chunk in track_stream(source(), usage)])
    assert raw.endswith(b"data: [DONE]\r\n\r\n")
    assert usage.prompt_tokens == 8
    assert usage.completion_tokens == 3
    assert saved[0]["outcome"] is UsageOutcome.SUCCEEDED


async def test_rewriter_hides_engine_path_and_preserves_non_data_frames() -> None:
    model_id = uuid.uuid4()

    async def source() -> AsyncIterator[bytes]:
        yield b": keepalive\n\n"
        yield b'data: {"model":"/private/model","choices":[]}\n\n'
        yield b"data: [DONE]\n\n"

    chunks = [chunk async for chunk in rewrite_openai_model(source(), model_id)]
    assert chunks[0] == b": keepalive\n\n"
    assert b"/private/model" not in b"".join(chunks)
    assert str(model_id).encode() in chunks[1]
    assert chunks[-1] == b"data: [DONE]\n\n"


async def test_cancelled_stream_finishes_usage_once(monkeypatch: pytest.MonkeyPatch) -> None:
    saved: list[dict[str, object]] = []

    async def persist(**kwargs: object) -> None:
        saved.append(kwargs)

    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", persist)

    async def cancelled() -> AsyncIterator[bytes]:
        yield b"data: partial\n\n"
        raise asyncio.CancelledError

    usage = UsageTracker(ANONYMOUS, str(uuid.uuid4()), GatewayProtocol.OPENAI)
    with pytest.raises(asyncio.CancelledError):
        async for _ in track_stream(cancelled(), usage):
            pass
    assert len(saved) == 1
    assert saved[0]["outcome"] is UsageOutcome.DISCONNECTED


async def test_first_token_metrics_are_recorded_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from coire_api.gateway import execution

    recorded: list[str] = []
    monkeypatch.setattr(
        execution,
        "first_token_duration_ms",
        SimpleNamespace(record=lambda _value, _attrs: recorded.append("first")),
    )
    monkeypatch.setattr(
        execution,
        "overhead_duration_ms",
        SimpleNamespace(record=lambda _value, _attrs: recorded.append("overhead")),
    )

    async def source() -> AsyncIterator[bytes]:
        yield b'data: {"choices":[]}\n\n'
        yield b"data: [DONE]\n\n"

    usage = UsageTracker(ANONYMOUS, str(uuid.uuid4()), GatewayProtocol.OPENAI)
    async def persist(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", persist)
    timing = StreamTiming()
    timing.upstream_started_at = perf_counter()
    timing.first_chunk_at = perf_counter()
    assert len([chunk async for chunk in track_stream(source(), usage, timing=timing)]) == 2
    assert recorded == ["first", "overhead"]
