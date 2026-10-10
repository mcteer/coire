"""Fresh credential checks gate output without competing with private admission."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from coire_api.auth import PrincipalKind
from coire_api.gateway import execution, proxy
from coire_api.gateway.proxy import EngineProxyError, StreamTiming
from coire_core.models.gateway import UsageOutcome
from coire_core.settings import Settings


def inputs() -> tuple[Mock, Mock]:
    usage = Mock(
        principal=Mock(api_key_id="synthetic", kind=PrincipalKind.API_KEY),
        finish=AsyncMock(),
    )
    request = Mock(
        is_disconnected=AsyncMock(return_value=False),
        app=SimpleNamespace(state=SimpleNamespace(settings=None)),
    )
    return usage, request


async def test_revocation_at_handoff_blocks_every_content_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage, request = inputs()
    check = AsyncMock(return_value=False)
    monkeypatch.setattr(execution, "_credential_is_active", check)
    admitted, release = asyncio.Event(), asyncio.Event()
    timing = StreamTiming(upstream_ready=asyncio.Event())

    async def source() -> AsyncIterator[bytes]:
        admitted.set()
        await release.wait()
        assert timing.upstream_ready is not None
        timing.upstream_ready.set()
        yield b'data: {"choices":[{"delta":{"content":"must not deliver"}}]}\n\n'

    async def collect() -> list[bytes]:
        return [chunk async for chunk in execution.track_stream(source(), usage, request, timing)]

    task = asyncio.create_task(collect())
    await asyncio.wait_for(admitted.wait(), 1)
    await asyncio.sleep(0)
    check.assert_not_awaited()
    release.set()
    chunks = await asyncio.wait_for(task, 1)
    assert b"must not deliver" not in b"".join(chunks)
    assert b"credential_revoked" in b"".join(chunks)
    check.assert_awaited_once_with(usage.principal)
    usage.finish.assert_awaited_once_with(UsageOutcome.REFUSED, failure_code="credential_revoked")


async def test_upstream_failure_cancels_unstarted_handoff_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage, request = inputs()
    check = AsyncMock(return_value=True)
    monkeypatch.setattr(execution, "_credential_is_active", check)
    timing = StreamTiming(upstream_ready=asyncio.Event())

    async def source() -> AsyncIterator[bytes]:
        await asyncio.sleep(0)
        if timing.upstream_started_at is None:
            raise EngineProxyError("synthetic admission failure")
        yield b": upstream started\n\n"

    async def collect() -> list[bytes]:
        return [chunk async for chunk in execution.track_stream(source(), usage, request, timing)]

    assert await asyncio.wait_for(collect(), 1) == []
    check.assert_not_awaited()
    usage.finish.assert_awaited_once_with(UsageOutcome.FAILED, failure_code="engine_stream_failed")


async def test_direct_source_still_rechecks_before_forwarding_first_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage, request = inputs()
    check = AsyncMock(return_value=True)
    monkeypatch.setattr(execution, "_credential_is_active", check)
    timing = StreamTiming(upstream_ready=asyncio.Event())

    async def source() -> AsyncIterator[bytes]:
        yield b": upstream chunk\n\n"

    chunks = [chunk async for chunk in execution.track_stream(source(), usage, request, timing)]
    assert chunks == [b": upstream chunk\n\n"]
    check.assert_awaited_once_with(usage.principal)
    usage.finish.assert_awaited_once_with(UsageOutcome.SUCCEEDED)


async def test_ordinary_stream_keeps_early_credential_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage, request = inputs()
    checked, release = asyncio.Event(), asyncio.Event()

    async def check(_principal: object) -> bool:
        checked.set()
        return True

    monkeypatch.setattr(execution, "_credential_is_active", check)

    async def source() -> AsyncIterator[bytes]:
        await release.wait()
        yield b": ordinary chunk\n\n"

    async def collect() -> list[bytes]:
        return [
            chunk
            async for chunk in execution.track_stream(source(), usage, request, StreamTiming())
        ]

    task = asyncio.create_task(collect())
    await asyncio.wait_for(checked.wait(), 1)
    release.set()
    assert await asyncio.wait_for(task, 1) == [b": ordinary chunk\n\n"]


async def test_proxy_signals_handoff_after_lease_commit_before_engine_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = asyncio.Event()
    timing = StreamTiming(upstream_ready=ready)
    committed = False

    @asynccontextmanager
    async def slot(_url: str, _settings: Settings) -> AsyncIterator[None]:
        yield

    @asynccontextmanager
    async def lease(_url: str, _settings: Settings) -> AsyncIterator[None]:
        nonlocal committed
        assert not ready.is_set()
        committed = True
        yield

    async def lines() -> AsyncIterator[str]:
        yield "data: [DONE]"

    @asynccontextmanager
    async def upstream(*_args: object, **_kwargs: object) -> AsyncIterator[Mock]:
        assert committed and ready.is_set() and timing.upstream_started_at is not None
        yield Mock(raise_for_status=Mock(), aiter_lines=lines)

    monkeypatch.setattr(proxy, "engine_slot", slot)
    monkeypatch.setattr(proxy, "request_lease", lease)
    monkeypatch.setattr(proxy, "_client", lambda: SimpleNamespace(stream=upstream))
    assert [
        chunk async for chunk in proxy.stream("http://fixture.invalid", {}, Settings(), timing)
    ] == [b"data: [DONE]\n\n"]
