"""Authenticated health probes refresh the agent identity after a node rollout."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import httpx
import pytest

from coire_api.db import NodeRow
from coire_api.nodes_prober import NodeProber
from coire_core.models.node import Reachability
from coire_core.net import ControlClient
from coire_core.settings import Settings


@pytest.mark.parametrize("reported_name", ["coire-edge-a", "coire-edge-b"])
async def test_probe_refreshes_version_only_for_the_expected_node(reported_name: str) -> None:
    row = cast(
        NodeRow,
        SimpleNamespace(
            name="coire-edge-a",
            agent_version="0.1.0",
            reachability=Reachability.UNKNOWN,
            probe_failures=0,
            last_seen_at=None,
        ),
    )
    body = {
        "name": reported_name,
        "agent_version": "0.2.0",
        "uptime_seconds": 10,
        "cpu_percent": 1,
        "memory_total_bytes": 1000,
        "memory_free_bytes": 500,
        "disk_total_bytes": 1000,
        "disk_free_bytes": 500,
        "agent_cpu_percent": 1,
        "agent_rss_bytes": 100,
        "collection_budget_ok": True,
        "path": "mesh",
        "sampled_at": datetime.now(UTC).isoformat(),
    }
    client = cast(
        ControlClient,
        SimpleNamespace(get=AsyncMock(return_value=httpx.Response(200, json=body))),
    )
    settings = cast(
        Settings,
        SimpleNamespace(node_listen_port=9400, node_probe_failures_before_unreachable=3),
    )
    observed = await NodeProber(settings)._probe_node(client, row, "node-token")
    if reported_name == row.name:
        assert observed is not None
        assert row.agent_version == "0.2.0"
        assert row.reachability is Reachability.HEALTHY
    else:
        assert observed is None
        assert row.agent_version == "0.1.0"
        assert row.probe_failures == 1
