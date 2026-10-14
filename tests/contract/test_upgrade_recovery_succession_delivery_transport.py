"""Metadata delivery is authenticated, scoped, enabled and never cached."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coire_api.app import create_app
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoverySuccessionDeliveryRead,
    UpgradeExpiredRecoverySuccessionDeliveryState,
)
from coire_core.settings import Settings


@pytest.mark.parametrize("fault", [None, "missing", "wrong", "foreign", "disabled", "extra"])
def test_delivery_callback(monkeypatch, fault):
    from coire_api.routes import internal_upgrades
    from coire_api.upgrades import expired_recovery_succession_delivery as module

    monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
    session = object()

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(internal_upgrades, "session_scope", scope)
    body = UpgradeExpiredRecoverySuccessionDeliveryRead(
        succession_id="01ARZ3NDEKTSV4RRFFQ69G5FAX",
        predecessor_sha256="a" * 64,
        successor={"pid": 12345, "create_time_s": 100.0, "environment_sha256": "b" * 64},
    )
    service = AsyncMock(
        return_value=UpgradeExpiredRecoverySuccessionDeliveryState(
            request=body, grant=None, checked_at=datetime.now(UTC)
        )
    )
    monkeypatch.setattr(module, "read_succession_delivery", service)
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
    data = body.model_dump(mode="json")
    if fault == "extra":
        data["deadline_at"] = "2099-01-01T00:00:00Z"
    response = client.post(
        "/api/v1/internal/upgrades/01ARZ3NDEKTSV4RRFFQ69G5FAY/recovery-succession-delivery",
        json=data,
        headers=headers,
    )
    assert (
        response.status_code
        == {None: 200, "missing": 401, "wrong": 401, "foreign": 401, "disabled": 503, "extra": 422}[
            fault
        ]
    )
    assert "private-test-token" not in response.text
    if fault is None:
        assert response.headers["cache-control"] == "no-store"
        service.assert_awaited_once_with(
            session, "01ARZ3NDEKTSV4RRFFQ69G5FAY", body, "coire-edge-a"
        )
    else:
        service.assert_not_awaited()
