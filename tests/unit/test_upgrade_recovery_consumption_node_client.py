"""Fixed Core consumption callback never trusts a changed or redirected receipt."""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from test_upgrade_expired_recovery_transfer import transfer_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerTransfer,
    UpgradeExpiredRecoveryTransferConsumption,
)
from coire_core.settings import Settings


@pytest.mark.parametrize("fault", [None, "authenticated_route", "changed", "redirect", "extra"])
def test_fixed_consumption_transport_matches_exact_receipt(tmp_path, monkeypatch, fault):
    from coire_node.upgrade_guard import NativeGuardTransport

    transfer = UpgradeExpiredRecoveryOwnerTransfer.model_validate(transfer_payload(tmp_path))
    receipt = UpgradeExpiredRecoveryTransferConsumption(
        consumption_id=uuid4(),
        transfer=transfer,
        consumer=transfer.successor,
        consumed_at=transfer.issued_at,
    )
    api = None
    service = None
    if fault == "authenticated_route":
        from fastapi.testclient import TestClient

        from coire_api.app import create_app
        from coire_api.upgrades import expired_recovery_consumption

        monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
        service = AsyncMock(return_value=receipt)
        monkeypatch.setattr(expired_recovery_consumption, "verify_live_consumption", service)

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
        assert (
            request.url.path
            == f"/api/v1/internal/upgrades/{transfer.intent.recovery_id}/recovery-consumption"
        )
        assert request.headers["X-Coire-Node"] == "coire-edge-a"
        assert request.headers["Authorization"] == "Bearer fixture-token"
        if api is not None:
            response = api.post(
                request.url.path, json=json.loads(request.content), headers=dict(request.headers)
            )
            assert response.headers["Cache-Control"] == "no-store"
            return httpx.Response(response.status_code, json=response.json())
        data = receipt.model_dump(mode="json")
        if fault == "changed":
            data["consumption_id"] = str(uuid4())
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
                control.verify_recovery_consumption(receipt)
        else:
            assert control.verify_recovery_consumption(receipt) == receipt
    assert len(calls) == 1
    if service is not None:
        service.assert_awaited_once()
        assert service.await_args.args[1:] == (
            transfer.intent.recovery_id,
            receipt.sha256(),
            receipt.consumer,
            "coire-edge-a",
        )
