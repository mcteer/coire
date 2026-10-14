"""Guard failure reporting authenticates before validation and scopes its route."""

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coire_api.app import create_app
from coire_core.settings import Settings


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong", "X-Coire-Node": "coire-edge-a"},
        {"Authorization": "Bearer fixture-token", "X-Coire-Node": "coire-edge-b"},
    ],
)
def test_native_failure_replay_rejects_missing_or_foreign_credentials_before_body(
    monkeypatch, headers
):
    monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
    app = create_app(
        Settings(upgrades_enabled=True, node_tokens=SecretStr('{"coire-edge-a":"fixture-token"}'))
    )
    response = TestClient(app).post(
        "/api/v1/internal/upgrades/01ARZ3NDEKTSV4RRFFQ69G5FAV/native-failure-receipts",
        json={},
        headers=headers,
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_native_failure_replay_authenticated_body_stays_strict(monkeypatch):
    monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
    app = create_app(
        Settings(upgrades_enabled=True, node_tokens=SecretStr('{"coire-edge-a":"fixture-token"}'))
    )
    response = TestClient(app).post(
        "/api/v1/internal/upgrades/01ARZ3NDEKTSV4RRFFQ69G5FAV/native-failure-receipts",
        json={},
        headers={"Authorization": "Bearer fixture-token", "X-Coire-Node": "coire-edge-a"},
    )
    assert response.status_code == 422
