"""Cancellation credentials precede body parsing and database access."""

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coire_api.app import create_app
from coire_api.upgrades.recovery_support import create_recovery_support
from coire_core.settings import Settings


@pytest.mark.parametrize("purpose", ["unclaimed-cancellation", "native-terminal"])
@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("surface", ["admin", "internal"])
def test_no_credentials_never_reach_cancellation_authority(monkeypatch, scoped, surface, purpose):
    monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
    settings = Settings(
        upgrades_enabled=True,
        node_tokens=SecretStr('{"coire-edge-a":"fixture-token"}'),
        upgrade_recovery_support_job_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        upgrade_recovery_support_generation=57,
        upgrade_recovery_support_source_sha256="a" * 64,
    )
    app = create_recovery_support(settings) if scoped else create_app(settings)
    path = (
        f"/api/v1/{surface}/upgrades/"
        + ("recoveries/" if surface == "admin" and purpose == "native-terminal" else "")
        + f"01ARZ3NDEKTSV4RRFFQ69G5FAW/{purpose}"
    )
    client = TestClient(app)
    response = client.post(path, json={}) if surface == "admin" else client.get(path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_correct_node_credential_reaches_exact_cancellation_reader(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from datetime import UTC, datetime
    from uuid import uuid4

    from upgrade_native_fixtures import native_plan

    from coire_core.models.upgrade_unclaimed import (
        UpgradeUnclaimedCancellationGrant,
        UpgradeUnclaimedCancellationState,
    )

    plan, _, _ = native_plan(tmp_path)
    grant = UpgradeUnclaimedCancellationGrant(
        plan=plan, requested_by=uuid4(), requested_at=datetime.now(UTC)
    )

    @asynccontextmanager
    async def session():
        yield None

    async def read(_session, job_id, node):
        assert job_id == plan.operation.job_id and node == "coire-edge-a"
        return UpgradeUnclaimedCancellationState(grant=grant, observed_at=datetime.now(UTC))

    monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
    monkeypatch.setattr("coire_api.routes.internal_upgrades.session_scope", session)
    monkeypatch.setattr(
        "coire_api.upgrades.unclaimed_cancellation.read_unclaimed_cancellation", read
    )
    app = create_app(
        Settings(upgrades_enabled=True, node_tokens=SecretStr('{"coire-edge-a":"fixture-token"}'))
    )
    try:
        response = TestClient(app).get(
            f"/api/v1/internal/upgrades/{plan.operation.job_id}/unclaimed-cancellation",
            headers={"Authorization": "Bearer fixture-token", "X-Coire-Node": "coire-edge-a"},
        )
        assert response.status_code == 200
        assert UpgradeUnclaimedCancellationState.model_validate(response.json()).grant == grant
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)


@pytest.mark.parametrize("fault", [None, "lost", "foreign"])
async def test_unclaimed_sdk_pins_exact_preparation_and_never_retries(tmp_path, fault):
    import asyncio
    from datetime import UTC, datetime
    from uuid import uuid4

    import httpx
    from upgrade_native_fixtures import native_plan

    from coire_core.client import UpgradeClient
    from coire_core.errors import UpgradeConflict, UpgradeUnavailable
    from coire_core.models.upgrade_unclaimed import (
        UpgradeUnclaimedCancellationGrant,
        UpgradeUnclaimedCancellationRequest,
    )

    plan, _, _ = await asyncio.to_thread(native_plan, tmp_path)
    request = UpgradeUnclaimedCancellationRequest(plan_sha256=plan.authority_sha256())
    foreign = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
    outgoing_plan = (
        plan
        if fault != "foreign"
        else plan.model_copy(
            update={
                "operation": plan.operation.model_copy(update={"job_id": foreign}),
                "maintenance": plan.maintenance.model_copy(update={"job_id": foreign}),
            }
        )
    )
    grant = UpgradeUnclaimedCancellationGrant(
        plan=outgoing_plan, requested_by=uuid4(), requested_at=datetime.now(UTC)
    )
    calls = []

    def handle(outgoing):
        calls.append(outgoing)
        assert (
            outgoing.url.path
            == f"/api/v1/admin/upgrades/{plan.operation.job_id}/unclaimed-cancellation"
        )
        if fault == "lost":
            raise httpx.ReadError("lost reply", request=outgoing)
        return httpx.Response(200, json=grant.model_dump(mode="json"))

    try:
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
            UpgradeClient("http://gateway.test", SecretStr("fixture-admin"), client=http) as client,
        ):
            if fault:
                with pytest.raises(UpgradeUnavailable if fault == "lost" else UpgradeConflict):
                    await client.request_unclaimed_cancellation(plan.operation.job_id, request)
            else:
                assert (
                    await client.request_unclaimed_cancellation(plan.operation.job_id, request)
                    == grant
                )
        assert len(calls) == 1
    finally:

        def unseal():
            for path in tmp_path.rglob("*"):
                if path.is_dir() and not path.is_symlink():
                    path.chmod(0o700)

        await asyncio.to_thread(unseal)


def test_correct_native_terminal_node_credential_reaches_scoped_reader(monkeypatch):
    from contextlib import asynccontextmanager

    from coire_core.errors import UpgradeUnavailable

    @asynccontextmanager
    async def session():
        yield None

    seen = []

    async def read(_session, recovery_id, node):
        seen.append((recovery_id, node))
        raise UpgradeUnavailable("contract reader reached")

    monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
    monkeypatch.setattr("coire_api.routes.internal_upgrades.session_scope", session)
    monkeypatch.setattr("coire_api.upgrades.native_terminal.native_terminal_state", read)
    app = create_app(
        Settings(upgrades_enabled=True, node_tokens=SecretStr('{"coire-edge-a":"fixture-token"}'))
    )
    response = TestClient(app).get(
        "/api/v1/internal/upgrades/01ARZ3NDEKTSV4RRFFQ69G5FAW/native-terminal",
        headers={"Authorization": "Bearer fixture-token", "X-Coire-Node": "coire-edge-a"},
    )
    assert response.status_code == 503 and seen == [("01ARZ3NDEKTSV4RRFFQ69G5FAW", "coire-edge-a")]
