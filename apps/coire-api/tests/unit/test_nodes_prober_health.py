from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from coire_api.nodes_prober import NodeProber
from coire_core.models.node import Reachability
from coire_core.settings import Settings


@pytest.mark.asyncio
async def test_failed_probe_does_not_refresh_previous_observation() -> None:
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    row = SimpleNamespace(
        name="coire-edge-a",
        reachability=Reachability.HEALTHY,
        probe_failures=0,
        probe_successes=1,
        probe_degraded=0,
        heartbeat_latency_ms=1.0,
        last_observed_at=observed_at,
        last_seen_at=observed_at,
        last_observation={"name": "old"},
    )

    class Client:
        async def get(self, *_: Any, **__: Any) -> Any:
            raise OSError("offline")

    prober = NodeProber(Settings(node_probe_failures_before_unreachable=1))
    await prober._probe_node(Client(), row, "token")  # type: ignore[arg-type]
    assert row.reachability is Reachability.UNREACHABLE
    assert row.last_observed_at == observed_at
    assert row.last_observation == {"name": "old"}
