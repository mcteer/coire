from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from coire_api.gateway.execution import track_stream
from coire_core.models.gateway import UsageOutcome


@pytest.mark.asyncio
async def test_stream_terminates_with_auth_error_after_key_revocation(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def source() -> AsyncIterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"one"}}]}\n\n'
        yield b'data: {"choices":[{"delta":{"content":"two"}}]}\n\n'

    @asynccontextmanager
    async def scope():  # type: ignore[no-untyped-def]
        yield Mock()

    monkeypatch.setattr("coire_api.db.session_scope", scope)
    monkeypatch.setattr("coire_api.identity.keys.key_is_active", AsyncMock(return_value=False))
    principal = Mock(api_key_id=Mock())
    usage = Mock(
        principal=principal,
        protocol=Mock(value="openai"),
        finish=AsyncMock(),
    )
    request = Mock(
        is_disconnected=AsyncMock(return_value=False),
        app=SimpleNamespace(
            state=SimpleNamespace(settings=SimpleNamespace(credential_stream_recheck_s=1.0))
        ),
    )

    chunks = [chunk async for chunk in track_stream(source(), usage, request)]

    assert len(chunks) == 1
    assert b"credential_revoked" in chunks[0]
    assert chunks[0].endswith(b"data: [DONE]\n\n")
    usage.finish.assert_awaited_once_with(UsageOutcome.REFUSED, failure_code="credential_revoked")


@pytest.mark.asyncio
async def test_first_key_recheck_runs_while_upstream_waits(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    checked = asyncio.Event()

    async def source() -> AsyncIterator[bytes]:
        await asyncio.wait_for(checked.wait(), timeout=1)
        yield b'data: {"choices":[]}\n\n'

    async def key_active(_session: object, _principal: object) -> bool:
        checked.set()
        return True

    @asynccontextmanager
    async def scope():  # type: ignore[no-untyped-def]
        yield Mock()

    monkeypatch.setattr("coire_api.db.session_scope", scope)
    monkeypatch.setattr("coire_api.identity.keys.key_is_active", key_active)
    usage = Mock(
        principal=Mock(api_key_id=Mock()), protocol=Mock(value="openai"), finish=AsyncMock()
    )
    request = Mock(
        is_disconnected=AsyncMock(return_value=False),
        app=SimpleNamespace(
            state=SimpleNamespace(settings=SimpleNamespace(credential_stream_recheck_s=1.0))
        ),
    )

    async def collect() -> list[bytes]:
        return [chunk async for chunk in track_stream(source(), usage, request)]

    assert await asyncio.wait_for(collect(), timeout=2) == [b'data: {"choices":[]}\n\n']
