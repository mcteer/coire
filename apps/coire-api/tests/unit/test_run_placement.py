from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import AgentRunRow, NodeRow
from coire_core.models.node import NodeRole, Reachability
from coire_core.models.runs import AgentRunState
from coire_scheduler.runs import choose_studio, rank_studio_candidates


def node(name: str, role: NodeRole, state: Reachability = Reachability.HEALTHY) -> NodeRow:
    return NodeRow(
        id=uuid.uuid4(),
        name=name,
        role=role,
        memory_total_bytes=1,
        disk_total_bytes=1,
        agent_version="test",
        reachability=state,
    )


def test_run_placement_never_returns_core_or_unhealthy_nodes() -> None:
    core = node("coire-core", NodeRole.CORE)
    down = node("coire-edge-a", NodeRole.STUDIO, Reachability.UNREACHABLE)
    studio = node("coire-edge-b", NodeRole.STUDIO)
    ranked = rank_studio_candidates([core, down, studio], {studio.id: 0}, set(), cap=3)
    assert ranked == [studio]


def test_run_placement_prefers_model_copy_then_fifo_capacity() -> None:
    local = node("coire-edge-b", NodeRole.STUDIO)
    emptier = node("coire-edge-a", NodeRole.STUDIO)
    ranked = rank_studio_candidates(
        [emptier, local],
        {local.id: 2, emptier.id: 0},
        {local.id},
        cap=3,
    )
    assert ranked == [local, emptier]
    assert rank_studio_candidates([local], {local.id: 3}, {local.id}, cap=3) == []


async def test_non_fifo_head_stays_queued(monkeypatch: pytest.MonkeyPatch) -> None:
    head_id, later_id = uuid.uuid4(), uuid.uuid4()
    run = AgentRunRow(
        id=later_id,
        requester_user_id=uuid.uuid4(),
        profile="general",
        primary_model_id=uuid.uuid4(),
        primary_variant_id=uuid.uuid4(),
        workspace_ref="workspace",
        token_scope={},
        state=AgentRunState.PLACING,
        limits={},
        resource_usage={},
    )

    class Session:
        async def get(self, *_: object, **__: object) -> AgentRunRow:
            return run

        async def scalar(self, *_: object, **__: object) -> uuid.UUID:
            return head_id

    @asynccontextmanager
    async def scope():  # type: ignore[no-untyped-def]
        yield cast(AsyncSession, Session())

    monkeypatch.setattr("coire_scheduler.runs.session_scope", scope)
    assert await choose_studio(later_id) is None
