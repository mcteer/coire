"""Node agent contract tests (T049).

Covers the three properties that make the two-listener design safe: the token is required,
the egress listener refuses unmarked traffic, and every fallback is counted and logged.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest
import yaml
from jsonschema import Draft202012Validator

from coire_core.models.node import NetworkPath, NodePath, NodeStatus, ThermalState
from coire_core.settings import Settings
from coire_node.agent import create_app

CONTRACT = Path(__file__).resolve().parents[4] / "specs/000-bootstrap/contracts/health-api.yaml"
TOKEN = "correct-horse-battery-staple"


@pytest.fixture(scope="session")
def node_status_validator() -> Draft202012Validator:
    contract = yaml.safe_load(CONTRACT.read_text())
    schema = contract["components"]["schemas"]["NodeStatus"]
    return Draft202012Validator(schema)


class StubCollector:
    def latest(self, *, path: NodePath = NodePath.MESH) -> NodeStatus:
        return NodeStatus(
            name="coire-edge-a",
            agent_version="0.1.0",
            uptime_seconds=1.0,
            cpu_percent=5.0,
            gpu_percent=42.0,
            thermal_state=ThermalState.NOMINAL,
            memory_total_bytes=274877906944,
            memory_free_bytes=200000000000,
            disk_total_bytes=1979120929996,
            disk_free_bytes=1800000000000,
            agent_cpu_percent=0.3,
            agent_rss_bytes=40 * 1024 * 1024,
            collection_budget_ok=True,
            path=path,
            sampled_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        )


def settings() -> Settings:
    s = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    return s.model_copy(update={"node_token": __import__("pydantic").SecretStr(TOKEN)})


async def call(
    listener: NodePath | NetworkPath, headers: dict[str, str] | None = None
) -> httpx.Response:
    app = create_app(settings(), StubCollector(), listener=listener)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://node") as client:
        return await client.get("/node/health", headers=headers or {})


AUTH = {"Authorization": f"Bearer {TOKEN}"}
FALLBACK = {"X-Coire-Path": "fallback"}


class TestAuthentication:
    async def test_no_token_is_401(self) -> None:
        assert (await call(NodePath.MESH)).status_code == 401

    async def test_wrong_token_is_401(self) -> None:
        resp = await call(NodePath.MESH, {"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    async def test_correct_token_is_200(self) -> None:
        assert (await call(NodePath.MESH, AUTH)).status_code == 200


class TestListenerSeparation:
    async def test_control_listener_is_authenticated_and_labelled(self) -> None:
        assert (await call(NetworkPath.CONTROL)).status_code == 401
        response = await call(NetworkPath.CONTROL, AUTH)
        assert response.status_code == 200
        assert response.json()["path"] == "control"

    async def test_data_listener_has_no_health_route(self) -> None:
        assert (await call(NetworkPath.DATA, AUTH)).status_code == 404

    async def test_egress_without_marker_is_403(self) -> None:
        """Platform traffic belongs on the mesh; using egress must be deliberate."""
        resp = await call(NodePath.FALLBACK, AUTH)
        assert resp.status_code == 403
        assert "mesh" in resp.json()["detail"]

    async def test_egress_with_marker_is_served_and_labelled(self) -> None:
        resp = await call(NodePath.FALLBACK, {**AUTH, **FALLBACK})
        assert resp.status_code == 200
        assert resp.json()["path"] == "fallback"

    async def test_mesh_response_is_labelled_mesh(self) -> None:
        assert (await call(NodePath.MESH, AUTH)).json()["path"] == "mesh"

    async def test_fallback_is_logged_at_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """FR-013c: sustained slow-path operation must never be silent."""
        with caplog.at_level(logging.WARNING, logger="coire_node.agent"):
            await call(NodePath.FALLBACK, {**AUTH, **FALLBACK})
        assert any("EGRESS path" in r.getMessage() for r in caplog.records)


class TestContractShape:
    async def test_matches_the_contract(self, node_status_validator: Draft202012Validator) -> None:
        body = (await call(NodePath.MESH, AUTH)).json()
        node_status_validator.validate(body)

    async def test_reports_the_agents_own_cost(self) -> None:
        """FR-012c: the agent accounts for what it takes from inference."""
        body = (await call(NodePath.MESH, AUTH)).json()
        assert "agent_cpu_percent" in body
        assert "agent_rss_bytes" in body
        assert body["collection_budget_ok"] is True
