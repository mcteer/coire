"""Cleanup peer transport remains private, disabled by default and strict."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import NAMESPACE_URL, uuid5

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from upgrade_fixtures import plan

from coire_api.app import create_app
from coire_core.models.upgrades import (
    NodeUpgradeCollectiveCleanup,
    NodeUpgradeCollectiveRead,
    NodeUpgradeCollectiveScope,
)
from coire_core.settings import Settings


@pytest.mark.parametrize("fault", ["missing", "wrong", "foreign", "disabled", "extra"])
def test_cleanup_peer_transport_refuses_before_service(monkeypatch, fault):
    from coire_api.upgrades import collective_cleanup

    monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
    service = AsyncMock()
    monkeypatch.setattr(collective_cleanup, "collective_cleanup_peer", service)
    value = plan(paired=True)
    read = NodeUpgradeCollectiveRead(
        generation=value.operation.generation,
        plan_sha256=value.authority_sha256(),
        runtime_sha256=value.target.manifest.environment_sha256,
        stage="candidate",
        deadline_at=datetime.now(UTC) + timedelta(seconds=120),
    )
    scope = NodeUpgradeCollectiveScope(
        plan=value,
        request=read,
        index=0,
        command_id=uuid5(
            NAMESPACE_URL,
            f"coire-upgrade-collective/{value.operation.job_id}/candidate/{read.plan_sha256}/0",
        ),
    )
    body = NodeUpgradeCollectiveCleanup(scope=scope).model_dump(mode="json")
    node = TestClient(
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
    response = node.post(
        f"/api/v1/internal/upgrades/{value.operation.job_id}/collective-cleanup-peer",
        json=body,
        headers=headers,
    )
    assert (
        response.status_code
        == {"missing": 401, "wrong": 401, "foreign": 401, "disabled": 503, "extra": 422}[fault]
    )
    assert "private-test-token" not in response.text
    service.assert_not_awaited()
