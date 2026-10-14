"""Owner callback exact binding, real authentication middleware and refusal paths."""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerClaim
from coire_core.settings import Settings


@pytest.mark.parametrize("operation", ["verify_recovery_owner", "accept_recovery_owner"])
@pytest.mark.parametrize(
    "fault", [None, "authenticated_route", "changed", "redirect", "extra", "scope"]
)
def test_fixed_owner_transport_matches_exact_claim(tmp_path, monkeypatch, fault, operation):
    from coire_node.upgrade_guard import NativeGuardTransport

    claim = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    intent = claim.consumption.transfer.intent
    api = None
    service = None
    if fault in {"authenticated_route", "scope"}:
        from fastapi.testclient import TestClient

        from coire_api.app import create_app
        from coire_api.upgrades import expired_recovery_owner

        monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
        service = AsyncMock(
            return_value=(claim, True) if operation == "accept_recovery_owner" else claim
        )
        if fault == "scope" and operation == "verify_recovery_owner":
            service.side_effect = UpgradeForbidden()
        monkeypatch.setattr(
            expired_recovery_owner,
            "accept_owner_claim" if operation == "accept_recovery_owner" else "verify_live_owner",
            service,
        )

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
        assert request.url.host == "core.private" and request.url.port == 8010
        suffix = (
            "recovery-owner-claim"
            if operation == "accept_recovery_owner"
            else "recovery-owner-check"
        )
        assert request.url.path == f"/api/v1/internal/upgrades/{intent.recovery_id}/{suffix}"
        assert request.headers["X-Coire-Node"] == "coire-edge-a"
        assert request.headers["Authorization"] == "Bearer fixture-token"
        assert json.loads(request.content) == (
            claim.model_dump(mode="json")
            if operation == "accept_recovery_owner"
            else {"owner_sha256": claim.sha256(), "consumer": claim.process.model_dump(mode="json")}
        )
        if api is not None:
            path = request.url.path
            if fault == "scope":
                path = path.replace(intent.recovery_id, "01ARZ3NDEKTSV4RRFFQ69G5FAX")
            response = api.post(
                path, json=json.loads(request.content), headers=dict(request.headers)
            )
            if fault != "scope":
                assert response.headers["Cache-Control"] == "no-store"
            return httpx.Response(response.status_code, json=response.json())
        data = claim.model_dump(mode="json")
        if fault == "changed":
            data["kernel_boot_identity"] = str(uuid4())
        elif fault == "extra":
            data["url"] = "http://foreign"
        return httpx.Response(
            302 if fault == "redirect" else 200,
            json=data,
            headers={"Location": "http://foreign"} if fault == "redirect" else {},
        )

    settings = Settings(
        node_name="coire-edge-a",
        node_control_host="127.0.0.1",
        core_control_host="core.private",
        core_api_port=8010,
        node_token=SecretStr("fixture-token"),
    )
    with httpx.Client(
        transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False
    ) as client:
        control = NativeGuardTransport(settings, client)
        if fault not in {None, "authenticated_route"}:
            with pytest.raises(UpgradeUnavailable):
                getattr(control, operation)(claim)
        else:
            assert getattr(control, operation)(claim) == claim
    assert len(calls) == 1
    if service is not None and fault == "scope" and operation == "accept_recovery_owner":
        service.assert_not_awaited()
    elif service is not None:
        service.assert_awaited_once()
        assert service.await_args.args[1:] == (
            (claim, "coire-edge-a")
            if operation == "accept_recovery_owner"
            else (
                "01ARZ3NDEKTSV4RRFFQ69G5FAX" if fault == "scope" else intent.recovery_id,
                claim.sha256(),
                claim.process,
                "coire-edge-a",
            )
        )
