from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import EngineProcessRow, NodeRow, PlacementCommandRow
from coire_api.nodes_client import NodeClient
from coire_api.registry.reconciler import ENGINE_START_GRACE_S, RegistryReconciler
from coire_core.models.engine import EngineState, ReconcileResult
from coire_core.models.node import NodeRole
from coire_core.settings import Settings


@pytest.mark.parametrize("age,expected", [(1, EngineState.STARTING), (31, EngineState.FAILED)])
async def test_node_reconcile_defers_only_transient_startup_miss(
    monkeypatch: pytest.MonkeyPatch, age: int, expected: EngineState
) -> None:
    node = NodeRow(
        id=uuid.uuid4(),
        name="coire-edge-a",
        role=NodeRole.STUDIO,
        memory_total_bytes=1,
        disk_total_bytes=1,
        agent_version="test",
    )
    engine = EngineProcessRow(
        id=uuid.uuid4(),
        node_id=node.id,
        port=9500,
        state=EngineState.STARTING,
        started_at=datetime.now(UTC) - timedelta(seconds=age),
    )

    class Results:
        def __init__(self, rows: list[object]) -> None:
            self.rows = rows

        def scalar_one_or_none(self) -> object | None:
            return self.rows[0] if self.rows else None

        def scalars(self) -> Results:
            return self

        def all(self) -> list[object]:
            return self.rows

    class Session:
        async def scalar(self, query: object) -> object | None:
            return None

        async def execute(self, query: object) -> Results:
            return Results([node] if "FROM nodes" in str(query) else [engine])

    class Client:
        async def reconcile(self, name: str, request: object) -> ReconcileResult:
            assert name == node.name
            return ReconcileResult(dead=[engine.id])

    reconciler = RegistryReconciler(Settings(_secrets_dir="/none"))  # type: ignore[call-arg]
    reconciler._node_reconcile.add(node.name)
    failures: list[uuid.UUID] = []

    async def fail_instance(_: AsyncSession, row: EngineProcessRow, reason: str) -> None:
        failures.append(row.id)

    async def audit(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(reconciler, "_fail_instance_for_engine", fail_instance)
    monkeypatch.setattr("coire_api.registry.reconciler.write_audit", audit)
    await reconciler._reconcile_nodes(cast(AsyncSession, Session()), cast(NodeClient, Client()))
    assert ENGINE_START_GRACE_S < 31
    assert engine.state is expected
    assert failures == ([] if expected is EngineState.STARTING else [engine.id])


@pytest.mark.parametrize(
    "state,age,expected",
    [
        ("running", 64, True),
        ("running", 601, False),
        ("succeeded", 64, False),
        ("failed", 64, False),
        ("pending", 64, False),
    ],
)
async def test_checksum_preflight_deferral_is_bounded_by_actual_load_command(
    state: str, age: int, expected: bool
) -> None:
    engine = EngineProcessRow(
        id=uuid.uuid4(),
        node_id=uuid.uuid4(),
        port=9500,
        state=EngineState.STARTING,
        started_at=datetime.now(UTC) - timedelta(seconds=64),
    )
    command = PlacementCommandRow(
        id=uuid.uuid4(),
        decision_id=uuid.uuid4(),
        engine_id=engine.id,
        node_id=engine.node_id,
        operation="load",
        state=state,
        updated_at=datetime.now(UTC) - timedelta(seconds=age),
    )

    class Session:
        async def scalar(self, query: object) -> PlacementCommandRow:
            sql = str(query)
            assert "placement_commands.engine_id" in sql
            assert "placement_commands.node_id" in sql
            assert "placement_commands.operation" in sql
            return command

    reconciler = RegistryReconciler(Settings(_secrets_dir="/none"))  # type: ignore[call-arg]
    assert (
        await reconciler._engine_start_in_flight(cast(AsyncSession, Session()), engine) is expected
    )
    engine.state = EngineState.READY
    assert not await reconciler._engine_start_in_flight(cast(AsyncSession, Session()), engine)
