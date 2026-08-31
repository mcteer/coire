from __future__ import annotations

import pytest

from coire_api.gateway import proxy


@pytest.mark.asyncio
async def test_engine_client_is_reused_and_closed() -> None:
    await proxy.close_engine_client()
    proxy.init_engine_client()
    first = proxy._client()
    assert proxy._client() is first

    await proxy.close_engine_client()
    assert first.is_closed
