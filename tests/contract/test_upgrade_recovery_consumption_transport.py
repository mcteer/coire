"""Fresh recovery effect gate is private, scoped, strict and disabled by default."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coire_api.app import create_app
from coire_core.settings import Settings


@pytest.mark.parametrize("fault", ["missing", "wrong", "foreign", "disabled", "extra"])
def test_recovery_consumption_transport_refuses_before_service(monkeypatch, fault):
    from coire_api.upgrades import expired_recovery_consumption

    monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
    service = AsyncMock()
    monkeypatch.setattr(expired_recovery_consumption, "verify_live_consumption", service)
    body = {
        "consumption_sha256": "a" * 64,
        "consumer": {"pid": 12345, "create_time_s": 100.0, "environment_sha256": "b" * 64},
    }
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
        body["url"] = "http://foreign"
    response = client.post(
        "/api/v1/internal/upgrades/01ARZ3NDEKTSV4RRFFQ69G5FAW/recovery-consumption",
        json=body,
        headers=headers,
    )
    assert (
        response.status_code
        == {"missing": 401, "wrong": 401, "foreign": 401, "disabled": 503, "extra": 422}[fault]
    )
    assert "private-test-token" not in response.text
    service.assert_not_awaited()
