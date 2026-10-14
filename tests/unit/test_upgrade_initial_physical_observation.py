"""Initial Source and recovery-owner reads share the original freshness window."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from upgrade_bootstrap_fixtures import prepared_bootstrap

from coire_core.errors import UpgradeUnavailable
from coire_upgrade_executor.source_observer import InitialBootstrapObserver


@pytest.mark.parametrize("expired", [False, True])
async def test_initial_physical_reads_overlap_and_preserve_oldest_timestamp(expired):
    seed, proof = prepared_bootstrap()
    source_started, executor_started = asyncio.Event(), asyncio.Event()
    timestamp = datetime.now(UTC) - timedelta(seconds=6 if expired else 1)

    async def source():
        source_started.set()
        await executor_started.wait()
        return proof.source.model_copy(update={"observed_at": timestamp})

    async def executor(container_id, *, require_healthy):
        assert container_id == seed.executor_container_id and require_healthy is False
        executor_started.set()
        await source_started.wait()
        return datetime.now(UTC), proof.executor_started_at

    observer = InitialBootstrapObserver.__new__(InitialBootstrapObserver)
    observer.seed = seed
    observer.source = Mock(observe=AsyncMock(side_effect=source))
    observer.executor = Mock(verify_container=AsyncMock(side_effect=executor))
    if expired:
        with pytest.raises(UpgradeUnavailable, match="physical observations expired"):
            await asyncio.wait_for(observer.observe(), timeout=1)
    else:
        result = await asyncio.wait_for(observer.observe(), timeout=1)
        assert result.observed_at == timestamp
        assert result.source.observed_at == timestamp
        assert result.executor_container_id == seed.executor_container_id
