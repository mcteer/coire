"""Real API middleware authenticates immutable support's passive publication."""

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coire_api.app import create_app
from coire_core.settings import Settings


@pytest.mark.parametrize(
    "fault", [None, "missing", "wrong", "foreign", "scope", "disabled", "extra"]
)
def test_initial_observation_uses_fixed_authenticated_metadata_ingress(
    tmp_path, monkeypatch, fault
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "unit"))
    from test_upgrade_recovery_initial_relay import observation

    from coire_api.routes import internal_upgrades
    from coire_api.upgrades import expired_recovery_initial_relay as module

    proof = observation(tmp_path)
    gate = AsyncMock()
    monkeypatch.setattr(module, "verify_observation_context", gate)
    monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)

    @asynccontextmanager
    async def scope():
        yield object()

    monkeypatch.setattr(internal_upgrades, "session_scope", scope)
    app = create_app(
        Settings(
            upgrades_enabled=fault != "disabled",
            node_tokens=SecretStr('{"coire-edge-a":"fixture-token"}'),
        )
    )
    headers = {"X-Coire-Node": "coire-edge-a", "Authorization": "Bearer fixture-token"}
    if fault == "missing":
        headers = {}
    elif fault == "wrong":
        headers["Authorization"] = "Bearer wrong"
    elif fault == "foreign":
        headers["X-Coire-Node"] = "coire-edge-b"
    identifier = "01ARZ3NDEKTSV4RRFFQ69G5FAY" if fault == "scope" else proof.request.original_job_id
    body = proof.model_dump(mode="json")
    if fault == "extra":
        body["destination"] = "http://caller.invalid"
    response = TestClient(app).post(
        f"/api/v1/internal/upgrades/{identifier}/recovery-initial-observation",
        json=body,
        headers=headers,
    )
    assert (
        response.status_code
        == {
            None: 200,
            "missing": 401,
            "wrong": 401,
            "foreign": 401,
            "scope": 409,
            "disabled": 503,
            "extra": 422,
        }[fault]
    )
    if fault is None:
        assert response.headers["cache-control"] == "no-store"
        assert (
            app.state.upgrade_initial_recovery_observations.read(proof.request, proof.support)
            == proof
        )
        gate.assert_awaited_once()
    else:
        gate.assert_not_awaited()
