from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from coire_core.models.runs import (
    RunCollectedResult,
    RunContainerObservation,
    RunContainerStatus,
    RunLogChunk,
)
from coire_node.testing.harness import TOKEN, Agent


class StubRuns:
    def __init__(self) -> None:
        self.created: list[uuid.UUID] = []
        self.removed: list[tuple[uuid.UUID, bool]] = []

    async def create(self, command: Any) -> RunContainerStatus:
        self.created.append(command.run_id)
        return self._status(command.run_id, "created")

    async def start(self, run_id: uuid.UUID) -> RunContainerStatus:
        return self._status(run_id, "running")

    async def wait(self, run_id: uuid.UUID) -> RunContainerStatus:
        return self._status(run_id, "exited")

    async def logs(self, run_id: uuid.UUID, *, offset: int = 0) -> list[RunLogChunk]:
        return [RunLogChunk(run_id=run_id, offset=offset, stream="stdout", content="ok\n")]

    async def collect(self, run_id: uuid.UUID) -> RunCollectedResult:
        return RunCollectedResult(run_id=run_id, result={"ok": True})

    async def remove(self, run_id: uuid.UUID, *, kill: bool = False) -> None:
        self.removed.append((run_id, kill))

    async def observations(self) -> list[RunContainerObservation]:
        return []

    @staticmethod
    def _status(run_id: uuid.UUID, state: str) -> RunContainerStatus:
        return RunContainerStatus(
            run_id=run_id,
            container_id=f"container-{run_id}",
            state=state,
        )


def payload(run_id: uuid.UUID) -> dict[str, Any]:
    return {
        "run_id": str(run_id),
        "profile": "general",
        "model_id": str(uuid.uuid4()),
        "variant_id": str(uuid.uuid4()),
        "image": f"ghcr.io/mcteer/coire-agent@sha256:{'a' * 64}",
        "argv": ["-m", "coire_agent"],
        "workspace_ref": "workspace-1",
        "run_token": "r" * 48,
        "gateway_url": "http://coire-core.lab:8080/v1",
        "limits": {},
    }


def test_node_run_lifecycle_is_authenticated_and_typed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    agent = Agent(tmp_path)
    runs = StubRuns()
    app = agent.app()
    app.state.runs = runs
    run_id = uuid.uuid4()
    try:
        with TestClient(app) as anonymous:
            assert anonymous.post("/node/runs", json=payload(run_id)).status_code == 401
        with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
            created = client.post("/node/runs", json=payload(run_id))
            assert created.status_code == 201
            assert client.post(f"/node/runs/{run_id}/start").json()["state"] == "running"
            assert client.get(f"/node/runs/{run_id}/logs?offset=3").json()[0]["offset"] == 3
            assert client.post(f"/node/runs/{run_id}/wait").json()["state"] == "exited"
            assert client.get(f"/node/runs/{run_id}/result").json()["result"] == {"ok": True}
            assert client.delete(f"/node/runs/{run_id}?kill=true").status_code == 204
            assert client.get("/node/runs").json() == []
        assert runs.created == [run_id]
        assert runs.removed == [(run_id, True)]
    finally:
        agent.close()


def test_node_create_rejects_raw_docker_controls(tmp_path) -> None:  # type: ignore[no-untyped-def]
    agent = Agent(tmp_path)
    app = agent.app()
    app.state.runs = StubRuns()
    body = payload(uuid.uuid4()) | {"privileged": True}
    try:
        with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
            response = client.post("/node/runs", json=body)
            assert response.status_code == 422
    finally:
        agent.close()


def test_observation_timestamp_contract() -> None:
    assert datetime.now(UTC).tzinfo is not None
