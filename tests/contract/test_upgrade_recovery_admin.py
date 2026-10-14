"""Initial recovery and its durable Location are strict, scoped and default-off."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_upgrade_admin import client
from upgrade_recovery_fixtures import retained_native_fixture

from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryStatus
from coire_core.models.upgrades import UpgradePhase
from coire_core.upgrade_recovery import capture_expired_native_recovery


def intent_fixture(tmp_path):
    plan, native, scope, request, now = retained_native_fixture(tmp_path)
    return capture_expired_native_recovery(
        request=request,
        operation=plan.operation.model_copy(update={"phase": UpgradePhase.RECOVERY_REQUIRED}),
        plan=plan,
        native=native,
        support=plan.target,
        scopes=(scope,),
        recovery_id="01ARZ3NDEKTSV4RRFFQ69G5FAW",
        accepted_by=uuid4(),
        now=now,
    )


@pytest.mark.parametrize("created", [True, False])
def test_initial_admin_acceptance_has_a_readable_durable_location(tmp_path, monkeypatch, created):
    from coire_api.routes import admin_upgrades
    from coire_api.upgrades import expired_recovery_orchestration, expired_recovery_status

    intent = intent_fixture(tmp_path)
    api, session, _ = client(monkeypatch)
    api.app.state.upgrade_initial_recovery_observations = object()
    sessions = object()
    monkeypatch.setattr(admin_upgrades, "session_factory", lambda: sessions)
    accept = AsyncMock(return_value=(intent, created))
    monkeypatch.setattr(expired_recovery_orchestration, "accept_with_observation", accept)
    response = api.post(
        "/api/v1/admin/upgrades/recoveries", json=intent.request.model_dump(mode="json")
    )
    assert response.status_code == (202 if created else 200)
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == intent.model_dump(mode="json")
    assert accept.await_args.args[0] is sessions
    status = UpgradeExpiredRecoveryStatus(
        intent=intent, phase="accepted", active=True, observed_at=intent.accepted_at
    )
    read = AsyncMock(return_value=status)
    monkeypatch.setattr(expired_recovery_status, "read_recovery_status", read)
    response = api.get(response.headers["location"])
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == status.model_dump(mode="json")
    assert read.await_args.args[0] is session
    assert read.await_args.args[2] == intent.recovery_id


@pytest.mark.parametrize("fault", ["disabled", "unauthorized", "extra", "path"])
def test_recovery_admin_refuses_without_entering_effect_service(tmp_path, monkeypatch, fault):
    from coire_api.upgrades import expired_recovery_orchestration, expired_recovery_status

    intent = intent_fixture(tmp_path)
    api, _, _ = client(monkeypatch, enabled=fault != "disabled", allowed=fault != "unauthorized")
    accept = AsyncMock()
    read = AsyncMock()
    monkeypatch.setattr(expired_recovery_orchestration, "accept_with_observation", accept)
    monkeypatch.setattr(expired_recovery_status, "read_recovery_status", read)
    body = intent.request.model_dump(mode="json")
    if fault == "extra":
        body["native"] = intent.original_plan.model_dump(mode="json")
    response = (
        api.post("/api/v1/admin/upgrades/recoveries", json=body)
        if fault != "path"
        else api.get("/api/v1/admin/upgrades/recoveries/invalid")
    )
    assert response.status_code == {"disabled": 503, "unauthorized": 403}.get(fault, 422)
    accept.assert_not_awaited()
    read.assert_not_awaited()
