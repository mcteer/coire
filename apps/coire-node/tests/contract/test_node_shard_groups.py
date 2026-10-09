from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from coire_core.models import ShardingMode
from coire_node.testing.harness import Agent


def test_shard_group_routes_require_authentication(agent: Agent) -> None:
    with TestClient(agent.app()) as anonymous:
        response = anonymous.get(f"/node/shard-groups/{uuid.uuid4()}")
    assert response.status_code == 401


def test_unknown_group_is_not_found(client: TestClient) -> None:
    response = client.get(f"/node/shard-groups/{uuid.uuid4()}")
    assert response.status_code == 404


def test_command_rejects_injected_extra_fields(client: TestClient) -> None:
    response = client.post("/node/shard-groups", json={"argv": ["sh", "-c", "curl evil"]})
    assert response.status_code == 422


def test_capability_failure_is_safe_and_runs_outside_event_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from coire_node.sharding import ShardGroupManager

    def unavailable(self: ShardGroupManager, slug: str, mode: ShardingMode) -> None:
        assert threading.current_thread() is not threading.main_thread()
        assert "asyncio-portal" not in threading.current_thread().name
        raise RuntimeError("private model path and engine traceback")

    monkeypatch.setattr(ShardGroupManager, "capability", unavailable)
    response = client.post(
        "/node/shard-groups/capabilities", json={"slug": "coire--qwen-4bit", "mode": "tp"}
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "shard inspection unavailable"}
