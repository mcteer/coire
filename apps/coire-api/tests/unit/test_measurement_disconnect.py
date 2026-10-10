"""Private generation observes disconnects without polling ASGI receive per frame."""

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import PrincipalKind
from coire_api.gateway.execution import track_stream
from coire_api.training.gateway_measurements import _gateway_request, measured_gateway_request
from coire_core.models.gateway import UsageOutcome


def scope() -> dict[str, object]:
    return {
        "type": "http",
        "method": "POST",
        "path": "/private-fixture",
        "headers": [],
        "app": SimpleNamespace(
            state=SimpleNamespace(settings=SimpleNamespace(credential_stream_recheck_s=1.0))
        ),
    }


async def test_disconnect_during_upstream_wait_refuses_content() -> None:
    messages: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    async def receive() -> dict[str, object]:
        return await messages.get()

    request = Request(scope(), receive=receive)
    ready = asyncio.Event()

    async def source() -> AsyncIterator[bytes]:
        await ready.wait()
        yield b'data: {"choices":[{"delta":{"content":"must not deliver"}}]}\n\n'

    usage = Mock(
        principal=Mock(api_key_id=None, kind=PrincipalKind.ADMIN),
        protocol=Mock(value="openai"),
        finish=AsyncMock(),
    )
    async with AsyncSession() as session, measured_gateway_request(request, 0.0, session):
        current = _gateway_request.get()
        assert current is not None
        guarded = current[0]

        async def collect() -> list[bytes]:
            return [chunk async for chunk in track_stream(source(), usage, guarded)]

        collecting = asyncio.create_task(collect())
        await messages.put({"type": "http.disconnect"})
        await asyncio.sleep(0)
        ready.set()
        assert await asyncio.wait_for(collecting, 1) == []
    usage.finish.assert_awaited_once_with(
        UsageOutcome.DISCONNECTED, failure_code="client_disconnected"
    )
    assert _gateway_request.get() is None


async def test_finished_request_cancels_pending_disconnect_receive() -> None:
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def receive() -> dict[str, object]:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    request = Request(scope(), receive=receive)
    async with AsyncSession() as session, measured_gateway_request(request, 0.0, session):
        await asyncio.wait_for(entered.wait(), 1)
    assert cancelled.is_set()
    assert _gateway_request.get() is None


async def test_broken_receive_fails_closed() -> None:
    async def receive() -> dict[str, object]:
        raise RuntimeError("synthetic receive failure")

    async with (
        AsyncSession() as session,
        measured_gateway_request(Request(scope(), receive=receive), 0.0, session),
    ):
        await asyncio.sleep(0)
        current = _gateway_request.get()
        assert current is not None and await current[0].is_disconnected()
