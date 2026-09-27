"""A long run wait must never serialize admitted Studio kill commands."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api import run_executor
from coire_api.run_executor import RunCommandExecutor
from coire_core.settings import Settings


@pytest.mark.asyncio
async def test_same_node_kills_have_separate_admitted_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = uuid.uuid4()
    commands = [(uuid.uuid4(), node) for _ in range(3)]
    started: set[uuid.UUID] = set()
    release = asyncio.Event()

    class Results:
        def tuples(self) -> list[tuple[uuid.UUID, uuid.UUID]]:
            return commands

    class Session:
        async def execute(self, _: object) -> Results:
            return Results()

    @asynccontextmanager
    async def scope():  # type: ignore[no-untyped-def]
        yield cast(AsyncSession, Session())

    async def execute(command_id: uuid.UUID) -> None:
        started.add(command_id)
        await release.wait()

    monkeypatch.setattr(run_executor, "session_scope", scope)
    settings = Settings(_secrets_dir="/none")  # type: ignore[call-arg]
    settings.run_concurrency_cap = 3
    executor = RunCommandExecutor(settings)
    monkeypatch.setattr(executor, "_execute_safely", execute)
    loop = asyncio.create_task(executor._run_kills())
    try:
        for _ in range(50):
            if len(started) == 3:
                break
            await asyncio.sleep(0.01)
        assert started == {item[0] for item in commands}
        assert len(executor._kills) == 3
    finally:
        executor._stop.set()
        release.set()
        await loop
        await asyncio.gather(*executor._kills.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_per_node_kill_lane_obeys_admission_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    node = uuid.uuid4()
    commands = [(uuid.uuid4(), node) for _ in range(3)]
    started: set[uuid.UUID] = set()
    release = asyncio.Event()

    class Results:
        def tuples(self) -> list[tuple[uuid.UUID, uuid.UUID]]:
            return commands

    class Session:
        async def execute(self, _: object) -> Results:
            return Results()

    @asynccontextmanager
    async def scope():  # type: ignore[no-untyped-def]
        yield cast(AsyncSession, Session())

    async def execute(command_id: uuid.UUID) -> None:
        started.add(command_id)
        await release.wait()

    monkeypatch.setattr(run_executor, "session_scope", scope)
    settings = Settings(_secrets_dir="/none")  # type: ignore[call-arg]
    settings.run_concurrency_cap = 1
    executor = RunCommandExecutor(settings)
    monkeypatch.setattr(executor, "_execute_safely", execute)
    loop = asyncio.create_task(executor._run_kills())
    try:
        for _ in range(50):
            if started:
                break
            await asyncio.sleep(0.01)
        assert len(started) == 1
        assert len(executor._kills) == 1
    finally:
        executor._stop.set()
        release.set()
        await loop
        await asyncio.gather(*executor._kills.values(), return_exceptions=True)
