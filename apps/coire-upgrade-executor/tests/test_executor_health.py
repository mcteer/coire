"""The separate executor cannot advertise recovery readiness before bootstrap."""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coire_core.settings import Settings


def test_health_requires_executor_secret_and_verified_bootstrap() -> None:
    from coire_upgrade_executor.main import create_app

    app = create_app(
        Settings(upgrades_enabled=True, upgrade_executor_token=SecretStr("private-test-token"))
    )
    client = TestClient(app)
    assert client.get("/ready").status_code == 401
    assert client.get("/ready", headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = client.get("/ready", headers={"Authorization": "Bearer private-test-token"})
    assert response.status_code == 503
    assert "private-test-token" not in response.text
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_disabled_executor_stays_unready() -> None:
    from coire_upgrade_executor.main import create_app

    client = TestClient(
        create_app(Settings(upgrade_executor_token=SecretStr("private-test-token")))
    )
    assert (
        client.get("/ready", headers={"Authorization": "Bearer private-test-token"}).status_code
        == 503
    )


def test_default_off_installation_remains_live_without_claiming_readiness() -> None:
    from coire_upgrade_executor.main import create_app

    app = create_app(Settings(upgrade_executor_token=SecretStr("private-test-token")))
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer private-test-token"}
        assert client.get("/live", headers=headers).status_code == 200
        assert client.get("/ready", headers=headers).status_code == 503
        assert app.state.bootstrap_authority is None
    assert app.state.bootstrap_supervisor is None


@pytest.mark.parametrize("age", [None, -1, 11, 0])
def test_readiness_requires_fresh_positive_bootstrap_observation(age: int | None) -> None:
    from coire_upgrade_executor.main import create_app

    app = create_app(
        Settings(upgrades_enabled=True, upgrade_executor_token=SecretStr("private-test-token"))
    )
    app.state.bootstrap_verified = True
    app.state.bootstrap_authority = Mock()
    app.state.bootstrap_verified_at = (
        datetime.now(UTC) - timedelta(seconds=age) if age is not None else None
    )
    response = TestClient(app).get("/ready", headers={"Authorization": "Bearer private-test-token"})
    assert response.status_code == (200 if age == 0 else 503)


@pytest.mark.parametrize("fault", ["absent", "lost"])
def test_fresh_readiness_boolean_cannot_substitute_for_held_writer(fault: str) -> None:
    from coire_core.errors import UpgradeUnavailable
    from coire_upgrade_executor.main import create_app

    app = create_app(
        Settings(upgrades_enabled=True, upgrade_executor_token=SecretStr("private-test-token"))
    )
    app.state.bootstrap_verified = True
    app.state.bootstrap_verified_at = datetime.now(UTC)
    if fault == "lost":
        app.state.bootstrap_authority = Mock(
            verify=Mock(side_effect=UpgradeUnavailable("lost owner"))
        )
    response = TestClient(app).get("/ready", headers={"Authorization": "Bearer private-test-token"})
    assert response.status_code == 503


@pytest.mark.parametrize("running", [False, True])
def test_liveness_cannot_grant_bootstrap_readiness(running: bool) -> None:
    from coire_upgrade_executor.main import create_app

    app = create_app(
        Settings(upgrades_enabled=True, upgrade_executor_token=SecretStr("private-test-token"))
    )
    app.state.bootstrap_supervisor = Mock(done=Mock(return_value=not running))
    client = TestClient(app)
    assert client.get("/live").status_code == 401
    response = client.get("/live", headers={"Authorization": "Bearer private-test-token"})
    assert response.status_code == (200 if running else 503)
    assert (
        client.get("/ready", headers={"Authorization": "Bearer private-test-token"}).status_code
        == 503
    )
    assert app.state.bootstrap_verified is False


async def test_passive_route_is_authenticated_and_has_no_ready_side_effect() -> None:
    from unittest.mock import AsyncMock

    from coire_core.models.upgrades import ExecutorPassiveReadiness
    from coire_upgrade_executor.main import create_app

    app = create_app(
        Settings(upgrades_enabled=True, upgrade_executor_token=SecretStr("private-test-token"))
    )
    client = TestClient(app)
    assert client.get("/passive").status_code == 401
    assert (
        client.get("/passive", headers={"Authorization": "Bearer private-test-token"}).status_code
        == 503
    )
    proof = ExecutorPassiveReadiness(
        preparation_sha256="a" * 64,
        candidate_seed_sha256="b" * 64,
        generation=2,
        candidate_runtime_id="candidate",
        candidate_container_id="c" * 64,
        candidate_image_id="sha256:" + "d" * 64,
        incumbent_runtime_id="incumbent",
        incumbent_container_id="e" * 64,
        observed_at=datetime.now(UTC),
    )
    observer = Mock(observe=AsyncMock(return_value=proof))
    app.state.passive_observer = observer
    response = client.get("/passive", headers={"Authorization": "Bearer private-test-token"})
    assert response.status_code == 200 and response.json() == proof.model_dump(mode="json")
    assert (
        client.get("/ready", headers={"Authorization": "Bearer private-test-token"}).status_code
        == 503
    )
    assert not app.state.bootstrap_verified
