"""Explicit fresh state, never HTTP errors or stale absence as retry authority."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerClaim
from coire_core.settings import Settings


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "unaccepted",
        "stale",
        "future",
        "redirect",
        "authenticated_route",
        "authenticated_unaccepted",
    ],
)
def test_owner_state_client_refuses_ambiguous_or_stale_probe(tmp_path, monkeypatch, fault):
    from coire_node.upgrade_guard import NativeGuardTransport

    claim = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    now = datetime.now(UTC)
    data = {
        "owner_sha256": claim.sha256(),
        "owner": None
        if fault in {"unaccepted", "authenticated_unaccepted"}
        else claim.model_dump(mode="json"),
        "checked_at": (
            now + timedelta(seconds=60 if fault == "future" else -60 if fault == "stale" else 0)
        ).isoformat(),
    }
    api = None
    service = None
    if fault in {"authenticated_route", "authenticated_unaccepted"}:
        from fastapi.testclient import TestClient

        from coire_api.app import create_app
        from coire_api.upgrades import expired_recovery_owner
        from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerState

        monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
        service = AsyncMock(return_value=UpgradeExpiredRecoveryOwnerState.model_validate(data))
        monkeypatch.setattr(expired_recovery_owner, "probe_owner_acceptance", service)

        @asynccontextmanager
        async def session():
            yield object()

        monkeypatch.setattr("coire_api.routes.internal_upgrades.session_scope", session)
        api = TestClient(
            create_app(
                Settings(
                    upgrades_enabled=True, node_tokens=SecretStr('{"coire-edge-a":"fixture-token"}')
                )
            )
        )
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path.endswith("/recovery-owner-state")
        assert request.url.host == "core.private"
        assert request.headers["Authorization"] == "Bearer fixture-token"
        assert request.headers["X-Coire-Node"] == "coire-edge-a"
        assert json.loads(request.content) == {
            "owner_sha256": claim.sha256(),
            "consumer": claim.process.model_dump(mode="json"),
        }
        if api is not None:
            response = api.post(
                request.url.path, json=json.loads(request.content), headers=dict(request.headers)
            )
            assert response.headers["Cache-Control"] == "no-store"
            return httpx.Response(response.status_code, json=response.json())
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
        transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False
    ) as client:
        control = NativeGuardTransport(settings, client)
        if fault in {"stale", "future", "redirect"}:
            with pytest.raises(UpgradeUnavailable):
                control.probe_recovery_owner(claim)
        else:
            observed = control.probe_recovery_owner(claim)
            assert observed.owner == (
                None if fault in {"unaccepted", "authenticated_unaccepted"} else claim
            )
    assert len(calls) == 1
    if service is not None:
        service.assert_awaited_once()
        assert service.await_args.args[1:] == (
            claim.consumption.transfer.intent.recovery_id,
            claim.sha256(),
            claim.process,
            "coire-edge-a",
        )
