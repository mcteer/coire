from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from coire_api.console import service
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

    async def fake_cluster(*_: object) -> ClusterState:
        return cluster

    async def fake_ledgers(_: object) -> list[MemoryLedger]:
        return [ledger]

    monkeypatch.setattr(service, "cluster_state", fake_cluster)
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
