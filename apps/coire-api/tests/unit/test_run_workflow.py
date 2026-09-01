from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import AgentRunRow, NodeRow, RunCommandRow
from coire_api.run_executor import RunCommandExecutor
from coire_api.runs import run_command_id
from coire_core.models.node import NodeRole, Reachability
from coire_core.models.runs import (
    AgentRunState,
    RunCommandState,
    RunContainerObservation,
    RunOperation,
)
from coire_core.settings import Settings


def test_run_command_identity_is_stable_across_replay() -> None:
    run_id = uuid.uuid4()
    assert run_command_id(run_id, RunOperation.CREATE) == run_command_id(
        run_id, RunOperation.CREATE
    )
    assert run_command_id(run_id, RunOperation.START) != run_command_id(run_id, RunOperation.CREATE)


async def test_create_replay_adopts_existing_labeled_container_without_remint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, node_id = uuid.uuid4(), uuid.uuid4()
    run = AgentRunRow(
        id=run_id,
        requester_user_id=uuid.uuid4(),
        profile="general",
        primary_model_id=uuid.uuid4(),
        primary_variant_id=uuid.uuid4(),
        node_id=node_id,
        workspace_ref="workspace",
        token_scope={},
        state=AgentRunState.CREATING,
        limits={},
        resource_usage={},
    )
    node = NodeRow(
        id=node_id,
        name="coire-edge-a",
        role=NodeRole.STUDIO,
        memory_total_bytes=1,
        disk_total_bytes=1,
        agent_version="test",
        reachability=Reachability.HEALTHY,
    )
    command_id = run_command_id(run_id, RunOperation.CREATE)
    command = RunCommandRow(
        id=command_id,
        run_id=run_id,
        node_id=node_id,
        operation=RunOperation.CREATE,
        attempt=1,
        state=RunCommandState.PENDING,
        detail={},
    )

    class Session:
        async def get(self, model: object, identifier: uuid.UUID):  # type: ignore[no-untyped-def]
            if identifier == command_id:
                return command
            if identifier == run_id:
                return run
            if identifier == node_id:
                return node
            return None

    @asynccontextmanager
    async def scope():  # type: ignore[no-untyped-def]
        yield cast(AsyncSession, Session())

    class Client:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def list_runs(self, _: str) -> list[RunContainerObservation]:
            return [
                RunContainerObservation(
                    run_id=run_id,
                    container_id="existing-container",
                    state="running",
                    observed_at=datetime.now(UTC),
                )
            ]

        async def create_run(self, *_: object) -> None:
            raise AssertionError("replay must not create a duplicate")

    monkeypatch.setattr("coire_api.run_executor.session_scope", scope)
    monkeypatch.setattr("coire_api.run_executor.NodeClient", Client)
    executor = RunCommandExecutor(
        Settings(_secrets_dir="/none")  # type: ignore[call-arg]
    )
    result = await executor._execute(command_id)
    assert result["container_id"] == "existing-container"
