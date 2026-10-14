from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoverySuccessionObservation,
)
from coire_core.settings import Settings


@pytest.mark.parametrize(
    "fault", [None, "foreign", "stale", "future", "redirect", "expired", "ingress", "lost"]
)
def test_observation_publisher_fixed_metadata_only(tmp_path, monkeypatch, fault):
    from coire_node.upgrade_guard import NativeGuardTransport

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    intent = owner.consumption.transfer.intent
    now = owner.claimed_at + timedelta(seconds=1)
    proof = UpgradeExpiredRecoverySuccessionObservation(
        succession_id="01ARZ3NDEKTSV4RRFFQ69G5FAX",
        previous_owner=owner,
        successor=owner.process.model_copy(update={"pid": owner.process.pid + 1}),
        kernel_boot_identity=owner.kernel_boot_identity,
        preserved_source=owner.legacy_stop.preserved_source,
        observed_at=now,
        previous_owner_retired=True,
    )
    if fault == "expired":
        now = intent.deadline_at
    elif fault == "stale":
        now += timedelta(seconds=6)
    elif fault == "future":
        now -= timedelta(seconds=1)
    monkeypatch.setattr("coire_node.upgrade_guard.datetime", SimpleNamespace(now=lambda zone: now))
    api = None
    app = None
    if fault == "ingress":
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock

        from fastapi.testclient import TestClient

        from coire_api.app import create_app
        from coire_api.upgrades import expired_recovery_observation_relay, expired_recovery_owner

        monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
        monkeypatch.setattr(
            expired_recovery_observation_relay, "datetime", SimpleNamespace(now=lambda zone: now)
        )
        monkeypatch.setattr(
            expired_recovery_owner, "verify_live_owner", AsyncMock(return_value=owner)
        )

        @asynccontextmanager
        async def session():
            yield object()

        monkeypatch.setattr("coire_api.routes.internal_upgrades.session_scope", session)
        app = create_app(
            Settings(
                upgrades_enabled=True, node_tokens=SecretStr('{"coire-edge-a":"fixture-token"}')
            )
        )
        api = TestClient(app)
    calls = []

    def handle(request):
        calls.append(request)
        assert (
            request.url.host == "core.private"
            and request.url.path
            == f"/api/v1/internal/upgrades/{intent.recovery_id}/recovery-succession-observation"
        )
        assert request.headers["Authorization"] == "Bearer fixture-token"
        if fault == "lost":
            raise httpx.ReadError("fixture lost response", request=request)
        data = proof.model_dump(mode="json")
        if api is not None:
            response = api.post(request.url.path, json=data, headers=dict(request.headers))
            assert response.headers["cache-control"] == "no-store"
            return httpx.Response(response.status_code, json=response.json())
        if fault == "foreign":
            data["successor"]["pid"] += 1
        return httpx.Response(
            302 if fault == "redirect" else 200,
            json=data,
            headers={"Location": "http://foreign"} if fault == "redirect" else {},
        )

    settings = Settings(
        node_name="coire-edge-a",
        node_control_host="127.0.0.1",
        core_control_host="core.private",
        node_token=SecretStr("fixture-token"),
    )
    with httpx.Client(
        transport=httpx.MockTransport(handle), trust_env=False, follow_redirects=False
    ) as client:
        control = NativeGuardTransport(settings, client)
        if fault and fault != "ingress":
            with pytest.raises(UpgradeUnavailable):
                control.publish_recovery_succession_observation(proof)
        else:
            assert control.publish_recovery_succession_observation(proof) == proof
    assert len(calls) == (0 if fault in {"stale", "future", "expired"} else 1)
    if app is not None:
        assert app.state.upgrade_succession_observations.read(owner, proof.succession_id) == proof
