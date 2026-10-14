"""Native peer observation is private, strict, disabled by default and read-only."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coire_api.app import create_app
from coire_core.models.upgrades import NodeUpgradePeerRead
from coire_core.settings import Settings

JOB = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


@pytest.fixture(autouse=True)
def local_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)


def test_peer_transport_refuses_missing_or_wrong_node_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_api.routes import internal_upgrades

    service = AsyncMock()
    monkeypatch.setattr(internal_upgrades, "guard_peer_inventory", service)
    settings = Settings(node_tokens=SecretStr('{"coire-edge-a":"private-test-token"}'))
    node = TestClient(create_app(settings))
    body = NodeUpgradePeerRead(generation=1, plan_sha256="a" * 64).model_dump(mode="json")
    route = f"/api/v1/internal/upgrades/{JOB}/peer-inventory"
    assert node.post(route, json=body).status_code == 401
    assert (
        node.post(
            route,
            json=body,
            headers={"X-Coire-Node": "coire-edge-a", "Authorization": "Bearer wrong"},
        ).status_code
        == 401
    )
    assert (
        node.post(
            route,
            json=body,
            headers={"X-Coire-Node": "coire-edge-b", "Authorization": "Bearer private-test-token"},
        ).status_code
        == 401
    )
    response = node.post(
        route,
        json=body,
        headers={"X-Coire-Node": "coire-edge-a", "Authorization": "Bearer private-test-token"},
    )
    assert response.status_code == 503
    assert "private-test-token" not in response.text
    service.assert_not_awaited()


def test_peer_request_cannot_choose_a_peer_url_path_or_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_api.routes import internal_upgrades

    service = AsyncMock()
    monkeypatch.setattr(internal_upgrades, "guard_peer_inventory", service)
    node = TestClient(
        create_app(
            Settings(
                upgrades_enabled=True,
                node_tokens=SecretStr('{"coire-edge-a":"private-test-token"}'),
            )
        )
    )
    response = node.post(
        f"/api/v1/internal/upgrades/{JOB}/peer-inventory",
        json={
            "generation": 1,
            "plan_sha256": "a" * 64,
            "peer": "coire-edge-b",
            "url": "http://foreign",
            "command": "launch",
        },
        headers={"X-Coire-Node": "coire-edge-a", "Authorization": "Bearer private-test-token"},
    )
    assert response.status_code == 422
    service.assert_not_awaited()


@pytest.mark.parametrize("fault", ["missing", "wrong", "foreign", "disabled", "extra"])
def test_collective_callback_is_scoped_authenticated_and_strict(monkeypatch, fault):
    from datetime import UTC, datetime, timedelta

    from coire_api.upgrades import collective
    from coire_core.models.upgrades import NodeUpgradeCollectiveRead

    service = AsyncMock()
    monkeypatch.setattr(collective, "qualify_collective", service)
    node = TestClient(
        create_app(
            Settings(
                upgrades_enabled=fault != "disabled",
                node_tokens=SecretStr('{"coire-edge-a":"private-test-token"}'),
            )
        )
    )
    body = NodeUpgradeCollectiveRead(
        generation=1,
        plan_sha256="a" * 64,
        runtime_sha256="b" * 64,
        stage="candidate",
        deadline_at=datetime.now(UTC) + timedelta(seconds=120),
    ).model_dump(mode="json")
    headers = {"X-Coire-Node": "coire-edge-a", "Authorization": "Bearer private-test-token"}
    if fault == "missing":
        headers = {}
    elif fault == "wrong":
        headers["Authorization"] = "Bearer wrong"
    elif fault == "foreign":
        headers["X-Coire-Node"] = "coire-edge-b"
    elif fault == "extra":
        body["command"] = "caller supplied command"
    response = node.post(f"/api/v1/internal/upgrades/{JOB}/collective", json=body, headers=headers)
    assert (
        response.status_code
        == {"missing": 401, "wrong": 401, "foreign": 401, "disabled": 503, "extra": 422}[fault]
    )
    assert "private-test-token" not in response.text
    service.assert_not_awaited()
