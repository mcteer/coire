import asyncio
import uuid

import pytest

from coire_api.gateway.loading import LoadCoordinator


@pytest.mark.asyncio
async def test_concurrent_requests_share_one_load() -> None:
    coordinator = LoadCoordinator()
    model_id = uuid.uuid4()
    calls = 0

    async def loader() -> None:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)

    await asyncio.gather(*(coordinator.run(model_id, loader) for _ in range(5)))
    assert calls == 1


@pytest.mark.asyncio
async def test_failure_is_shared_and_next_request_can_retry() -> None:
    coordinator = LoadCoordinator()
    model_id = uuid.uuid4()
    calls = 0

    async def loader() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("load failed")

    results = await asyncio.gather(
        coordinator.run(model_id, loader),
        coordinator.run(model_id, loader),
        return_exceptions=True,
    )
    assert all(isinstance(result, RuntimeError) for result in results)
    with pytest.raises(RuntimeError):
        await coordinator.run(model_id, loader)
    assert calls == 2
