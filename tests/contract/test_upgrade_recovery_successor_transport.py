"""Fixed authenticated successor callbacks never allow error fallback."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coire_api.app import create_app
from coire_core.settings import Settings


@pytest.mark.parametrize("endpoint", ["recovery-successor-check", "recovery-successor-state"])
@pytest.mark.parametrize("fault", ["missing", "wrong", "foreign", "disabled", "extra"])
def test_successor_callback_refuses_before_service(monkeypatch, endpoint, fault):
    from coire_api.upgrades import expired_recovery_successor as module

    monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
    service = AsyncMock()
    monkeypatch.setattr(
        module,
        "verify_live_successor" if endpoint.endswith("check") else "probe_successor_acceptance",
        service,
    )
    client = TestClient(
        create_app(
            Settings(
                upgrades_enabled=fault != "disabled",
                node_tokens=SecretStr('{"coire-edge-a":"private-test-token"}'),
            )
        )
    )
    body = {
        "claim_sha256": "a" * 64,
        "consumer": {"pid": 12345, "create_time_s": 100.0, "environment_sha256": "b" * 64},
    }
    if endpoint == "recovery-successor-state":
        body["grant_sha256"] = "c" * 64
    headers = {"X-Coire-Node": "coire-edge-a", "Authorization": "Bearer private-test-token"}
    if fault == "missing":
        headers = {}
    elif fault == "wrong":
        headers["Authorization"] = "Bearer wrong"
    elif fault == "foreign":
        headers["X-Coire-Node"] = "coire-edge-b"
    elif fault == "extra":
        body["deadline_at"] = "2099-01-01T00:00:00Z"
    result = client.post(
        f"/api/v1/internal/upgrades/01ARZ3NDEKTSV4RRFFQ69G5FAX/{endpoint}",
        json=body,
        headers=headers,
    )
    assert (
        result.status_code
        == {"missing": 401, "wrong": 401, "foreign": 401, "disabled": 503, "extra": 422}[fault]
    )
    assert "private-test-token" not in result.text
    service.assert_not_awaited()


def test_positive_successor_state_uses_real_middleware(monkeypatch):
    from coire_api.routes import internal_upgrades
    from coire_api.upgrades import expired_recovery_successor
    from coire_core.models.upgrade_recovery import UpgradeExpiredRecoverySuccessorState
    from coire_core.models.upgrades import NodeAgentProcessIdentity

    monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
    session = object()

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(internal_upgrades, "session_scope", scope)
    state = UpgradeExpiredRecoverySuccessorState(
        claim_sha256="a" * 64, owner=None, checked_at=datetime.now(UTC)
    )
    service = AsyncMock(return_value=state)
    monkeypatch.setattr(expired_recovery_successor, "probe_successor_acceptance", service)
    client = TestClient(
        create_app(
            Settings(
                upgrades_enabled=True,
                node_tokens=SecretStr('{"coire-edge-a":"private-test-token"}'),
            )
        )
    )
    consumer = NodeAgentProcessIdentity(pid=12345, create_time_s=100.0, environment_sha256="b" * 64)
    result = client.post(
        "/api/v1/internal/upgrades/01ARZ3NDEKTSV4RRFFQ69G5FAX/recovery-successor-state",
        json={
            "claim_sha256": state.claim_sha256,
            "consumer": consumer.model_dump(mode="json"),
            "grant_sha256": "c" * 64,
        },
        headers={"X-Coire-Node": "coire-edge-a", "Authorization": "Bearer private-test-token"},
    )
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert result.json() == state.model_dump(mode="json")
    service.assert_awaited_once_with(
        session,
        "01ARZ3NDEKTSV4RRFFQ69G5FAX",
        state.claim_sha256,
        consumer,
        "coire-edge-a",
        grant_sha256="c" * 64,
    )
