"""Actual API middleware protects fixed recovery metadata and closure surfaces."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coire_api.app import create_app
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryColdResolutionState
from coire_core.settings import Settings


@pytest.mark.parametrize("fault", [None, "missing", "wrong", "foreign", "disabled", "extra"])
def test_resolution_state_is_scoped_authenticated_no_store_and_default_off(monkeypatch, fault):
    from coire_api.routes import internal_upgrades
    from coire_api.upgrades import expired_recovery_cold_resolution as service

    body = {"intent_sha256": "a" * 64, "closure_sha256": "b" * 64}
    result = UpgradeExpiredRecoveryColdResolutionState(**body, checked_at=datetime.now(UTC))
    gate = AsyncMock(return_value=result)
    monkeypatch.setattr(service, "read_resolution_state", gate)
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
    elif fault == "extra":
        body["command"] = "release"
    response = TestClient(app).post(
        "/api/v1/internal/upgrades/01ARZ3NDEKTSV4RRFFQ69G5FAW/recovery-cold-resolution-state",
        json=body,
        headers=headers,
    )
    assert (
        response.status_code
        == {None: 200, "missing": 401, "wrong": 401, "foreign": 401, "disabled": 503, "extra": 422}[
            fault
        ]
    )
    if fault is None:
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["resolution"] is None
        gate.assert_awaited_once()
        assert gate.call_args.args[1] == "01ARZ3NDEKTSV4RRFFQ69G5FAW"
        assert gate.call_args.args[3] == "coire-edge-a"
    else:
        gate.assert_not_awaited()


@pytest.mark.parametrize(
    "suffix",
    [
        "recovery-cold-claim",
        "recovery-cold-check",
        "recovery-cold-state",
        "recovery-cold-smoke",
        "recovery-cold-verification",
        "recovery-cold-verification-check",
        "recovery-cold-handback",
        "recovery-cold-handback-check",
        "recovery-cold-handback-receipt",
        "recovery-cold-resolve",
    ],
)
def test_every_cold_effect_ingress_requires_node_credential_before_body_or_database(
    monkeypatch, suffix
):
    monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
    app = create_app(
        Settings(upgrades_enabled=True, node_tokens=SecretStr('{"coire-edge-a":"fixture-token"}'))
    )
    response = TestClient(app).post(
        f"/api/v1/internal/upgrades/01ARZ3NDEKTSV4RRFFQ69G5FAW/{suffix}", json={}
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
