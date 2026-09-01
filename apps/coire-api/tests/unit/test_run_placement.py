from __future__ import annotations

import uuid

from coire_api.db import NodeRow
from coire_core.models.node import NodeRole, Reachability
from coire_scheduler.runs import rank_studio_candidates


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
