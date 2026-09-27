from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from coire_api.console import service
from coire_api.routes.admin_console import _semantic_snapshot
from coire_core.models.console import ConsoleAlert, ConsoleCapabilities, ConsoleSnapshot
from coire_core.models.instance import ClusterNodeState, ClusterState
from coire_core.models.node import NodePath, NodeStatus, Reachability
from coire_core.models.placement import MemoryLedger
from coire_core.settings import Settings


@pytest.mark.asyncio
async def test_snapshot_projects_capacity_freshness_health_and_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    node_id = uuid.uuid4()
    cluster = ClusterState(
        observed_at=now,
        nodes=[
            ClusterNodeState(
                id=node_id,
                name="coire-edge-a",
                reachability=Reachability.UNREACHABLE,
                health_observed_at=now - timedelta(minutes=1),
                budget_bytes=90,
                reserved_bytes=50,
            )
        ],
        instances=[],
    )
    ledger = MemoryLedger(
        node_id=node_id,
        node_name="coire-edge-a",
        budget_bytes=90,
        sandbox_bytes=0,
        reserved_bytes=50,
        free_bytes=40,
        measured_resident_bytes=70,
        drift_ratio=0.4,
        health=Reachability.UNREACHABLE,
        health_reason="probe timed out",
        health_sampled_at=now - timedelta(minutes=1),
        updated_at=now,
    )
    status = NodeStatus(
        name="coire-edge-a",
        agent_version="test",
        uptime_seconds=1,
        cpu_percent=20,
        memory_total_bytes=100,
        memory_free_bytes=40,
        disk_total_bytes=1000,
        disk_free_bytes=800,
        agent_cpu_percent=1,
        agent_rss_bytes=1,
        collection_budget_ok=True,
        path=NodePath.MESH,
        sampled_at=now,
    )

    async def fake_cluster(*args: object) -> ClusterState:
        assert args[2] == [ledger]
        return cluster

    async def fake_ledgers(_: object) -> list[MemoryLedger]:
        return [ledger]

    monkeypatch.setattr(service, "project_cluster_state", fake_cluster)
    monkeypatch.setattr(service, "project_ledgers", fake_ledgers)
    app = SimpleNamespace(
        state=SimpleNamespace(reconciler=SimpleNamespace(node_statuses={"coire-edge-a": status}))
    )
    request = Request({"type": "http", "app": app})
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    settings.node_probe_interval_s = 5

    snapshot = await service.project_snapshot(
        request,
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        settings,
        observed_at=now,
    )

    projected = snapshot.cluster.nodes[0]
    assert projected.stale is True
    assert projected.health_reason == "probe timed out"
    assert projected.memory_total_bytes == 100
    assert projected.disk_free_bytes == 800
    assert {alert.title for alert in snapshot.alerts} == {
        "Ledger drift on coire-edge-a",
        "coire-edge-a is unreachable",
    }
    assert snapshot.cursor == str(int(now.timestamp() * 1000))


@pytest.mark.asyncio
async def test_console_cache_single_flight_copy_and_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    calls = 0
    snapshot = ConsoleSnapshot(
        observed_at=now,
        cursor="1",
        capabilities=ConsoleCapabilities(),
        cluster=ClusterState(observed_at=now, nodes=[], instances=[]),
        ledgers=[],
    )

    @asynccontextmanager
    async def scope():  # type: ignore[no-untyped-def]
        yield None

    async def project(*_: object, **__: object) -> ConsoleSnapshot:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return snapshot

    monkeypatch.setattr(service, "session_scope", scope)
    monkeypatch.setattr(service, "project_snapshot", project)
    app = SimpleNamespace(state=SimpleNamespace(console_snapshot_cache=service.SnapshotCache()))
    request = Request({"type": "http", "app": app})
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    projected = await asyncio.gather(
        *(service.cached_snapshot(request, None, settings) for _ in range(8))  # type: ignore[arg-type]
    )
    assert calls == 1
    projected[0].alerts.append(ConsoleAlert(severity="info", title="local", detail="copy"))
    assert projected[1].alerts == []
    app.state.console_snapshot_cache.expires_at = 0
    await service.cached_snapshot(request, None, settings)  # type: ignore[arg-type]
    assert calls == 2


def test_semantic_snapshot_ignores_refresh_clock_but_keeps_health_freshness() -> None:
    now = datetime.now(UTC)
    snapshot = ConsoleSnapshot(
        observed_at=now,
        cursor="1",
        capabilities=ConsoleCapabilities(),
        cluster=ClusterState(observed_at=now, nodes=[], instances=[]),
        ledgers=[],
    )
    later = snapshot.model_copy(deep=True)
    later.observed_at = now + timedelta(seconds=2)
    later.cursor = "2"
    later.cluster.observed_at = now + timedelta(seconds=2)
    assert _semantic_snapshot(snapshot) == _semantic_snapshot(later)
    later.alerts.append(ConsoleAlert(severity="warning", title="changed", detail="health"))
    assert _semantic_snapshot(snapshot) != _semantic_snapshot(later)
