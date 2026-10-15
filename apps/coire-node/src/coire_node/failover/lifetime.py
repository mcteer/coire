"""Lifetime fencing for node-owned upstream HTTP work and request accounting."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import HTTPException, Response
from fastapi.responses import StreamingResponse

from coire_core.failover_lifetime import (
    AuthorityCheck,
    FailoverAuthorityLost,
    check_authority,
    guarded_await,
)


async def bounded_response(
    work: Awaitable[Response],
    allowed: AuthorityCheck,
    release: Callable[[], None],
) -> Response:
    try:
        response = await guarded_await(work, allowed)
    except FailoverAuthorityLost as exc:
        release()
        raise HTTPException(503, "not_elected") from exc
    except BaseException:
        release()
        raise
    if not isinstance(response, StreamingResponse):
        release()
        return response
    source = aiter(response.body_iterator)

    async def guarded() -> AsyncIterator[bytes | str | memoryview]:
        pending: asyncio.Task[bytes | str | memoryview] | None = None
        try:
            while await check_authority(allowed):
                if pending is None:

                    async def next_chunk() -> bytes | str | memoryview:
                        return await anext(source)

                    pending = asyncio.create_task(next_chunk())
                done, _ = await asyncio.wait({pending}, timeout=0.5)
                if not done:
                    continue
                try:
                    chunk = pending.result()
                except StopAsyncIteration:
                    return
                pending = None
                if not await check_authority(allowed):
                    break
                yield chunk
            yield b'event: error\ndata: {"reason":"not_elected","coire_interrupted":true}\n\n'
        finally:
            try:
                if pending is not None:
                    pending.cancel()
                    with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                        await pending
                close = getattr(source, "aclose", None)
                if close is not None:
                    await close()
            finally:
                release()

    response.body_iterator = guarded()
    return response
