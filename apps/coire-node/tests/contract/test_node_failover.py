from __future__ import annotations

from fastapi.testclient import TestClient
from pydantic import SecretStr

from coire_core.models.node import NodePath
from coire_node.testing.harness import TOKEN, Agent


def test_failover_relay_uses_its_own_scoped_credential(agent: Agent) -> None:
    agent.settings.failover_relay_token = SecretStr("relay-only-token")
    with TestClient(agent.app(NodePath.MESH)) as client:
        # The broad node registration credential cannot access failover relay operations.
        response = client.get(
            "/node/failover/resident", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert response.status_code == 401
        response = client.get(
            "/node/failover/resident", headers={"X-Coire-Failover-Relay": "relay-only-token"}
        )
    assert response.status_code == 200
    assert response.json() == []


def test_fallback_listener_does_not_expose_failover_relay(agent: Agent) -> None:
    with TestClient(agent.app(NodePath.FALLBACK)) as client:
        response = client.get(
            "/node/failover/resident",
            headers={"X-Coire-Path": "fallback", "X-Coire-Failover-Relay": "relay-only-token"},
        )
    assert response.status_code == 404


def test_election_vote_without_a_participant_fails_closed(agent: Agent) -> None:
    with TestClient(agent.app(NodePath.MESH)) as client:
        response = client.post("/node/failover/election/votes", json={})
    assert response.status_code == 401
