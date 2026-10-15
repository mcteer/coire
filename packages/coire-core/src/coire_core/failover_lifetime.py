"""Cancel buffered upstream work promptly when bounded authority is lost."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Awaitable, Callable


class FailoverAuthorityLost(ValueError):
    """The caller must report an interrupted request, never a successful completion."""


type AuthorityCheck = Callable[[], bool | Awaitable[bool]]


async def check_authority(allowed: AuthorityCheck) -> bool:
    result = allowed()
    return await result if inspect.isawaitable(result) else result


async def guarded_await[T](work: Awaitable[T], allowed: AuthorityCheck) -> T:
    # Complete an asynchronous residency probe before scheduling any engine work.
    try:
        if not await check_authority(allowed):
            raise FailoverAuthorityLost("request authority lost")
    except BaseException:
        if inspect.iscoroutine(work):
            work.close()
        raise
    started = False

    async def run() -> T:
        nonlocal started
        started = True
        return await work

    task = asyncio.create_task(run())
    try:
        while await check_authority(allowed):
            done, _ = await asyncio.wait({task}, timeout=0.5)
            if done:
                if not await check_authority(allowed):
                    raise FailoverAuthorityLost("request authority expired")
                return task.result()
        raise FailoverAuthorityLost("request authority lost")
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if not started and inspect.iscoroutine(work):
            work.close()
