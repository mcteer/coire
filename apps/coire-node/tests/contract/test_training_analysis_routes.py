"""Analysis commands remain authenticated control-fabric operations with safe errors."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coire_core.models.node import NetworkPath
from coire_node.agent import create_app
from coire_node.testing.harness import TOKEN, Agent
from coire_node.training.analysis_supervisor import AnalysisSupervisor


def test_analysis_auth_listener_missing_identity_and_control_binding(tmp_path: Path) -> None:
    agent = Agent(tmp_path / "node", node_name="coire-edge-a")
    supervisor = AnalysisSupervisor(agent.settings, agent.reservations)
    identity = uuid.uuid4()
    path = f"/node/training/analyses/{identity}"
    try:
        control = create_app(
            agent.settings,
            agent.collector,
            listener=NetworkPath.CONTROL,
            training_analyses=supervisor,
        )
        data = create_app(
            agent.settings, agent.collector, listener=NetworkPath.DATA, training_analyses=supervisor
        )
        with TestClient(control) as client:
            assert client.get(path).status_code == 401
            headers = {"Authorization": f"Bearer {TOKEN}"}
            assert client.get(path, headers=headers).status_code == 404
            response = client.post(
                path + "/cancel",
                headers=headers,
                json={"analysis_id": str(uuid.uuid4()), "command_id": str(uuid.uuid4())},
            )
            assert response.status_code == 409
            assert (
                client.post(
                    path + "/cancel",
                    headers=headers,
                    json={
                        "analysis_id": str(identity),
                        "command_id": str(uuid.uuid4()),
                        "unsafe": True,
                    },
                ).status_code
                == 422
            )
        with TestClient(data) as client:
            assert client.get(path, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 404
    finally:
        agent.close()


def test_corrupt_journal_returns_safe_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = Agent(tmp_path / "node", node_name="coire-edge-a")
    supervisor = AnalysisSupervisor(agent.settings, agent.reservations)
    identity = uuid.uuid4()

    async def status(_identity: uuid.UUID) -> None:
        raise ValueError("private credential and source row")

    monkeypatch.setattr(supervisor, "status", status)
    try:
        app = create_app(
            agent.settings,
            agent.collector,
            listener=NetworkPath.CONTROL,
            training_analyses=supervisor,
        )
        with TestClient(app) as client:
            response = client.get(
                f"/node/training/analyses/{identity}", headers={"Authorization": f"Bearer {TOKEN}"}
            )
            assert response.status_code == 409
            assert "private" not in response.text
    finally:
        agent.close()
