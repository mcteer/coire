from __future__ import annotations

import uuid
from typing import Any

import pytest

from coire_api.placement.service import node_admission_locks
from coire_api.shard_executor import ShardCommandExecutor
from coire_core.settings import Settings


class Session:
    def __init__(self) -> None:
        self.keys: list[int] = []

    async def execute(self, statement: Any, params: dict[str, int]) -> None:
        self.keys.append(params["key"])


@pytest.mark.asyncio
async def test_group_locks_are_sorted_independent_of_request_order() -> None:
    left, right = uuid.uuid4(), uuid.uuid4()
    first, second = Session(), Session()
    async with node_admission_locks(first, [left, right]):  # type: ignore[arg-type]
        pass
    async with node_admission_locks(second, [right, left]):  # type: ignore[arg-type]
        pass
    assert first.keys == second.keys == sorted(first.keys)


@pytest.mark.asyncio
async def test_group_locks_reject_split_same_node_group() -> None:
    node = uuid.uuid4()
    with pytest.raises(ValueError):
        async with node_admission_locks(Session(), [node, node]):  # type: ignore[arg-type]
            pass


@pytest.mark.asyncio
async def test_shard_executor_survives_a_transient_queue_database_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent", placement_poll_interval_s=0.001
    )
    executor = ShardCommandExecutor(settings)
    calls = 0

    async def next_command() -> uuid.UUID | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database restarting")
        executor._stop.set()
        return None

    monkeypatch.setattr(executor, "_next_command", next_command)
    await executor._run()
    assert calls == 2
