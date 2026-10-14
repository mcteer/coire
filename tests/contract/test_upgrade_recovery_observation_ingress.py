from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coire_api.app import create_app
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoverySuccessionObservation,
)
from coire_core.settings import Settings


@pytest.mark.parametrize("fault", [None, "missing", "wrong", "foreign", "scope", "disabled"])
def test_trusted_observation_ingress_is_metadata_only(tmp_path, monkeypatch, fault):
    from coire_api.routes import internal_upgrades
    from coire_api.upgrades import expired_recovery_observation_relay as relay
    from coire_api.upgrades import expired_recovery_owner as module

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "unit"))
    from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

    previous = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    intent = previous.consumption.transfer.intent
    now = previous.claimed_at + timedelta(seconds=1)
    monkeypatch.setattr(relay, "datetime", SimpleNamespace(now=lambda zone: now))
    proof = UpgradeExpiredRecoverySuccessionObservation(
        succession_id="01ARZ3NDEKTSV4RRFFQ69G5FAX",
        previous_owner=previous,
        successor=previous.process.model_copy(update={"pid": previous.process.pid + 1}),
        kernel_boot_identity=previous.kernel_boot_identity,
        preserved_source=previous.legacy_stop.preserved_source,
        observed_at=now,
        previous_owner_retired=True,
    )
    session = object()

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(internal_upgrades, "session_scope", scope)
    monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
    gate = AsyncMock(return_value=previous)
    monkeypatch.setattr(module, "verify_live_owner", gate)
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
    identifier = "01ARZ3NDEKTSV4RRFFQ69G5FAY" if fault == "scope" else intent.recovery_id
    response = TestClient(app).post(
        f"/api/v1/internal/upgrades/{identifier}/recovery-succession-observation",
        json=proof.model_dump(mode="json"),
        headers=headers,
    )
    assert (
        response.status_code
        == {None: 200, "missing": 401, "wrong": 401, "foreign": 401, "scope": 403, "disabled": 503}[
            fault
        ]
    )
    if fault is None:
        assert response.headers["cache-control"] == "no-store"
        assert (
            app.state.upgrade_succession_observations.read(previous, proof.succession_id) == proof
        )
        gate.assert_awaited_once_with(
            session, intent.recovery_id, previous.sha256(), previous.process, "coire-edge-a"
        )
    else:
        gate.assert_not_awaited()
