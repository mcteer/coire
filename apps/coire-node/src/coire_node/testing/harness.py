"""A node agent wired for tests.

Lives in the package, like the other doubles, so both the contract tests and the integration
suite can build a fully-wired agent without duplicating the assembly that `serve()` does.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import SecretStr

from coire_core.models.node import NetworkPath, NodePath, NodeStatus, ThermalState
from coire_core.settings import Settings
from coire_node.engines import EngineManager
from coire_node.grants import Grants
from coire_node.jobs import JobSupervisor
from coire_node.reservations import ReservationLedger
from coire_node.store import Store

TOKEN = "test-node-token"


class StubCollector:
    """Stands in for MetricsCollector, reporting the feature-001 fields from real sources."""

    def __init__(self) -> None:
        self.store: Store | None = None
        self.jobs: JobSupervisor | None = None
        self.engines: EngineManager | None = None

    def attach(self, *, store: Store, jobs: JobSupervisor, engines: EngineManager) -> None:
        self.store, self.jobs, self.engines = store, jobs, engines

    def latest(self, *, path: NodePath = NodePath.MESH) -> NodeStatus:
        return NodeStatus(
            name="coire-edge-a",
            agent_version="0.2.0",
            uptime_seconds=1.0,
            cpu_percent=1.0,
            thermal_state=ThermalState.NOMINAL,
            memory_total_bytes=1024,
            memory_free_bytes=512,
            disk_total_bytes=1024,
            disk_free_bytes=512,
            agent_cpu_percent=0.1,
            agent_rss_bytes=1024,
            collection_budget_ok=True,
            path=path,
            sampled_at=datetime.now(UTC),
            engines=self.engines.statuses() if self.engines else [],
            jobs=self.jobs.active() if self.jobs else [],
            memory_budget_bytes=self.engines.budget_bytes() if self.engines else 0,
            memory_committed_bytes=self.engines.committed_bytes() if self.engines else 0,
            store_free_bytes=self.store.free_bytes() if self.store else 0,
        )


class Agent:
    """A node agent assembled the way `serve()` assembles one, minus the listeners."""

    def __init__(self, root: Path, **overrides: Any) -> None:
        root.mkdir(parents=True, exist_ok=True)
        # Defaults an override may replace, rather than duplicate — passing the same keyword
        # twice is a TypeError, not a last-one-wins.
        config: dict[str, Any] = {
            "node_store_dir": str(root / "models"),
            "node_state_dir": str(root / "state"),
            "node_hf_cache_dir": str(root / "hf"),
            "node_engine_start_timeout_s": 30.0,
            "node_engine_health_interval_s": 0.5,
            "disk_reserve_bytes": 0,
        }
        config.update(overrides)
        self.settings = Settings(_secrets_dir="/nonexistent", **config)  # type: ignore[call-arg]
        self.settings.node_token = SecretStr(TOKEN)
        self.store = Store(self.settings.node_store_dir)
        self.store.ensure_root()
        self.jobs = JobSupervisor(self.settings, self.store)
        self.grants = Grants()
        self.engines = EngineManager(self.settings, self.store, "127.0.0.1")
        self.reservations = ReservationLedger(
            self.settings, self.store, self.engines.committed_bytes
        )
        self.collector = StubCollector()
        self.collector.attach(store=self.store, jobs=self.jobs, engines=self.engines)

    def app(self, listener: NodePath | NetworkPath = NodePath.MESH) -> FastAPI:
        from coire_node.agent import create_app

        return create_app(
            self.settings,
            self.collector,
            listener=listener,
            store=self.store,
            jobs=self.jobs,
            engines=self.engines,
            grants=self.grants,
            reservations=self.reservations,
        )

    def client(self, listener: NodePath | NetworkPath = NodePath.MESH) -> Any:
        """A TestClient carrying this node's bearer (and the fallback marker when needed)."""
        from fastapi.testclient import TestClient

        client = TestClient(self.app(listener))
        client.headers.update({"Authorization": f"Bearer {TOKEN}"})
        if listener is NodePath.FALLBACK:
            client.headers.update({"X-Coire-Path": "fallback"})
        return client

    def close(self) -> None:
        self.engines.shutdown()
        self.jobs.shutdown()
