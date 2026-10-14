"""Unstarted metadata surfaces keep node/admin authentication and no-store."""

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coire_api.app import create_app
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerTransfer
from coire_core.settings import Settings


@pytest.mark.parametrize("fault", [None, "missing", "wrong", "foreign", "disabled", "extra"])
def test_unstarted_context_uses_fixed_node_metadata_ingress(tmp_path, monkeypatch, fault):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "unit"))
    from test_upgrade_expired_recovery_transfer import transfer_payload

    from coire_api.routes import internal_upgrades
    from coire_api.upgrades import expired_recovery_unstarted as service

    intent = UpgradeExpiredRecoveryOwnerTransfer.model_validate(transfer_payload(tmp_path)).intent
    gate = AsyncMock(return_value=(object(), intent))
    monkeypatch.setattr(service, "unstarted_context", gate)
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
    body = {"intent_sha256": intent.sha256()}
    if fault == "extra":
        body["recovery_s"] = 300
    response = TestClient(app).post(
        f"/api/v1/internal/upgrades/{intent.recovery_id}/recovery-unstarted-context",
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
        assert response.json() == intent.model_dump(mode="json")
        gate.assert_awaited_once()
    else:
        gate.assert_not_awaited()


@pytest.mark.parametrize(
    "route",
    [
        "/api/v1/internal/upgrades/01ARZ3NDEKTSV4RRFFQ69G5FAW/recovery-unstarted-context",
        "/api/v1/internal/upgrades/01ARZ3NDEKTSV4RRFFQ69G5FAW/recovery-unstarted-observation",
        "/api/v1/admin/upgrades/recoveries/01ARZ3NDEKTSV4RRFFQ69G5FAW/close-unstarted",
    ],
)
def test_unstarted_ingress_requires_credentials_before_body_or_database(monkeypatch, route):
    monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
    app = create_app(
        Settings(upgrades_enabled=True, node_tokens=SecretStr('{"coire-edge-a":"fixture-token"}'))
    )
    response = TestClient(app).post(route, json={})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
