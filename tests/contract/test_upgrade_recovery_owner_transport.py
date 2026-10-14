"""Owner effect gate is fixed-node, strict, disabled by default and no-store."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coire_api.app import create_app
from coire_core.settings import Settings


@pytest.mark.parametrize(
    "endpoint",
    [
        "recovery-owner-check",
        "recovery-owner-state",
        "recovery-support-context",
        "recovery-consumption-delivery",
    ],
)
@pytest.mark.parametrize("fault", ["missing", "wrong", "foreign", "disabled", "extra"])
def test_owner_transport_refuses_before_service(monkeypatch, fault, endpoint):
    from coire_api.upgrades import expired_recovery_owner

    monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
    service = AsyncMock()
    if endpoint == "recovery-consumption-delivery":
        from coire_api.upgrades import expired_recovery_delivery

        monkeypatch.setattr(expired_recovery_delivery, "read_consumption_state", service)
    monkeypatch.setattr(
        expired_recovery_owner,
        "read_live_support_context"
        if endpoint == "recovery-support-context"
        else "probe_owner_acceptance"
        if endpoint == "recovery-owner-state"
        else "verify_live_owner",
        service,
    )
    body = {
        "owner_sha256": "a" * 64,
        "consumer": {"pid": 12345, "create_time_s": 100.0, "environment_sha256": "b" * 64},
    }
    if endpoint == "recovery-support-context":
        body = {"protocol": 1}
    elif endpoint == "recovery-consumption-delivery":
        body["intent_sha256"] = body.pop("owner_sha256")
    client = TestClient(
        create_app(
            Settings(
                upgrades_enabled=fault != "disabled",
                node_tokens=SecretStr('{"coire-edge-a":"private-test-token"}'),
            )
        )
    )
    headers = {"X-Coire-Node": "coire-edge-a", "Authorization": "Bearer private-test-token"}
    if fault == "missing":
        headers = {}
    elif fault == "wrong":
        headers["Authorization"] = "Bearer wrong"
    elif fault == "foreign":
        headers["X-Coire-Node"] = "coire-edge-b"
    elif fault == "extra":
        body["deadline_at"] = "2099-01-01T00:00:00Z"
    response = client.post(
        f"/api/v1/internal/upgrades/01ARZ3NDEKTSV4RRFFQ69G5FAW/{endpoint}",
        json=body,
        headers=headers,
    )
    assert (
        response.status_code
        == {"missing": 401, "wrong": 401, "foreign": 401, "disabled": 503, "extra": 422}[fault]
    )
    assert "private-test-token" not in response.text
    service.assert_not_awaited()
