"""Shared stream execution keeps accounting and public transport stable."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from time import perf_counter
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import ANONYMOUS, Principal, PrincipalKind
from coire_api.gateway.execution import (
    canonical_text_payload,
    compatible_text_payload,
    load_and_resolve,
    rewrite_openai_model,
    track_stream,
)
from coire_api.gateway.loading import ModelLoadError
from coire_api.gateway.proxy import StreamTiming
from coire_api.gateway.resolution import ResolvedModel
from coire_api.gateway.usage import UsageTracker
from coire_core.models.gateway import (
    ChatCompletionRequest,
    ChatMessage,
    GatewayProtocol,
    UsageOutcome,
)
from coire_core.settings import Settings


def test_text_payload_adapters_keep_registry_path_private_and_preserve_content() -> None:
    model_id = uuid.uuid4()
    messages = [ChatMessage(role="user", content="hello")]
    request = ChatCompletionRequest(model=model_id, messages=messages, max_tokens=32)
    compatible = compatible_text_payload(request, "/owned/model")
    canonical = canonical_text_payload(messages, "/owned/model", output_tokens=32)
    assert compatible["model"] == canonical["model"] == "/owned/model"
    assert compatible["messages"] == canonical["messages"] == [{"role": "user", "content": "hello"}]
    assert compatible["max_tokens"] == canonical["max_tokens"] == 32
    assert compatible["stream"] is False
    assert canonical["stream"] is True
    assert canonical["stream_options"] == {"include_usage": True}
    assert "coire_wait_for_model" not in compatible


def test_shared_usage_binds_only_registry_resolved_identity() -> None:
    model_id = uuid.uuid4()
    engine_id = uuid.uuid4()
    usage = UsageTracker(ANONYMOUS, "caller-model", GatewayProtocol.OPENAI)
    usage.bind_resolution(
        ResolvedModel(model_id, "registry", None, "/owned/model", engine_id, None, None)
    )
    assert usage.model_id == model_id
    assert usage.engine_id == engine_id
    assert usage.node is None
    assert usage.requested_model_id == "caller-model"


async def test_cold_load_releases_transaction_before_engine_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_api.gateway import execution

    order: list[str] = []

    async def rollback() -> None:
        order.append("rollback")

    async def load(_model: uuid.UUID, _settings: Settings) -> None:
        order.append("load")

    async def resolve(*_args: object) -> ResolvedModel:
        order.append("resolve")
        return ResolvedModel(uuid.uuid4(), "test", None, None, None, None, None)

    session = SimpleNamespace(rollback=rollback, expire_all=lambda: order.append("expire"))
    monkeypatch.setattr(execution, "load_model", load)
    monkeypatch.setattr(execution, "resolve_model", resolve)
    settings = SimpleNamespace(gateway_wait_ceiling_s=1)
    await load_and_resolve(
        uuid.uuid4(), ANONYMOUS, cast(AsyncSession, session), cast(Settings, settings)
    )
    assert order == ["rollback", "load", "expire", "resolve"]


async def test_cold_load_refuses_revoked_run_before_resolving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_api.gateway import execution

    async def load(_model: uuid.UUID, _settings: Settings) -> None:
        return None

    resolve = AsyncMock()
    monkeypatch.setattr(execution, "load_model", load)
    monkeypatch.setattr(execution, "resolve_model", resolve)
    monkeypatch.setattr("coire_api.run_tokens.run_token_is_active", AsyncMock(return_value=False))
    session = SimpleNamespace(rollback=AsyncMock(), expire_all=lambda: None)
    principal = Principal(kind=PrincipalKind.RUN, run_id=uuid.uuid4())
    settings = SimpleNamespace(gateway_wait_ceiling_s=1)
    with pytest.raises(ModelLoadError, match="run credential revoked"):
        await load_and_resolve(
            uuid.uuid4(), principal, cast(AsyncSession, session), cast(Settings, settings)
        )
    resolve.assert_not_awaited()


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


async def test_rewriter_hides_path_across_fragmented_multiline_sse_frames() -> None:
    model_id = uuid.uuid4()
    raw = (
        b": keepalive\r\n\r\n"
        b"id: 7\r\nevent: completion\r\n"
        b'data: {"model":\r\n'
        b'data: "/private/model","choices":[]}\r\n\r\n'
        b"data: [DONE]\r\n\r\n"
    )

    async def source() -> AsyncIterator[bytes]:
        for index in range(0, len(raw), 5):
            yield raw[index : index + 5]

    output = b"".join([chunk async for chunk in rewrite_openai_model(source(), model_id)])
    assert b"/private/" not in output
    assert str(model_id).encode() in output
    assert b"id: 7\nevent: completion\n" in output
    assert b": keepalive\n\n" in output
    assert output.endswith(b"data: [DONE]\n\n")


async def test_rewriter_refuses_malformed_engine_frame_without_leaking_path() -> None:
    from coire_api.gateway.proxy import EngineProxyError

    async def source() -> AsyncIterator[bytes]:
        yield b'data: {"model":"/private/model"\n\n'

    with pytest.raises(EngineProxyError, match="invalid engine event"):
        _ = [chunk async for chunk in rewrite_openai_model(source(), uuid.uuid4())]


async def test_malformed_engine_frame_records_failed_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    saved: list[dict[str, object]] = []

    async def persist(**kwargs: object) -> None:
        saved.append(kwargs)

    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", persist)

    async def source() -> AsyncIterator[bytes]:
        yield b'data: {"model":"/private/model"\n\n'

    usage = UsageTracker(ANONYMOUS, str(uuid.uuid4()), GatewayProtocol.OPENAI)
    output = b"".join(
        [chunk async for chunk in track_stream(rewrite_openai_model(source(), uuid.uuid4()), usage)]
    )
    assert b"/private/model" not in output
    assert len(saved) == 1
    assert saved[0]["outcome"] is UsageOutcome.FAILED


async def test_rewriter_refuses_private_path_in_engine_error() -> None:
    from coire_api.gateway.proxy import EngineProxyError

    async def source() -> AsyncIterator[bytes]:
        yield b'data: {"error":{"message":"/private/model failed"}}\n\n'

    with pytest.raises(EngineProxyError, match="private model path"):
        _ = [
            chunk
            async for chunk in rewrite_openai_model(
                source(), uuid.uuid4(), private_model_path="/private/model"
            )
        ]


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

    recorded: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(
        execution,
        "first_token_duration_ms",
        SimpleNamespace(record=lambda _value, attrs: recorded.append(("first", attrs))),
    )
    monkeypatch.setattr(
        execution,
        "overhead_duration_ms",
        SimpleNamespace(record=lambda _value, attrs: recorded.append(("overhead", attrs))),
    )

    async def source() -> AsyncIterator[bytes]:
        yield b'data: {"choices":[]}\n\n'
        yield b"data: [DONE]\n\n"

    usage = UsageTracker(ANONYMOUS, str(uuid.uuid4()), GatewayProtocol.OPENAI)

    async def persist(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", persist)
    timing = StreamTiming()
    usage.node = "coire-edge-b"
    timing.upstream_started_at = perf_counter()
    timing.first_chunk_at = perf_counter()
    assert len([chunk async for chunk in track_stream(source(), usage, timing=timing)]) == 2
    assert recorded == [
        ("first", {"protocol": "openai", "node": "coire-edge-b"}),
        ("overhead", {"protocol": "openai", "node": "coire-edge-b"}),
    ]


async def test_disconnect_closes_upstream_before_return(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    closed = False

    async def source() -> AsyncIterator[bytes]:
        nonlocal closed
        try:
            yield b'data: {"choices":[]}\n\n'
        finally:
            closed = True

    async def persist(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", persist)
    usage = UsageTracker(ANONYMOUS, str(uuid.uuid4()), GatewayProtocol.OPENAI)
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=True))
    assert [chunk async for chunk in track_stream(source(), usage, request)] == []  # type: ignore[arg-type]
    assert closed


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
async def test_cold_stream_close_cancels_pending_load(
    monkeypatch: pytest.MonkeyPatch, protocol: str
) -> None:
    from coire_api.routes import v1

    cancelled = asyncio.Event()

    async def never_ready(*_args: object, **_kwargs: object) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(v1, "_load_and_resolve", never_ready)
    body = SimpleNamespace(model=uuid.uuid4(), coire_affinity_node=None)
    settings = SimpleNamespace(gateway_keepalive_interval_s=0.01)
    usage = UsageTracker(ANONYMOUS, str(body.model), GatewayProtocol.OPENAI)
    cold = v1._openai_cold_stream if protocol == "openai" else v1._anthropic_cold_stream
    source = cold(body, ANONYMOUS, object(), settings, usage, object(), StreamTiming())  # type: ignore[arg-type]
    assert await anext(source) == b": coire model loading\n\n"
    await cast(AsyncGenerator[bytes], source).aclose()
    assert cancelled.is_set()
