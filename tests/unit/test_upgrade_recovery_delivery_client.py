"""Fixed passive delivery refuses stale/future/error state, never fallback."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
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


@pytest.mark.parametrize(
    "fault", [None, "pending", "stale", "future", "redirect", "foreign", "authenticated"]
)
def test_delivery_client_binds_intent_actor_epoch(tmp_path, monkeypatch, fault):
    from coire_node.upgrade_guard import NativeGuardTransport

    transfer = UpgradeExpiredRecoveryOwnerTransfer.model_validate(transfer_payload(tmp_path))
    receipt = UpgradeExpiredRecoveryTransferConsumption(
        consumption_id=uuid4(),
        transfer=transfer,
        consumer=transfer.successor,
        consumed_at=transfer.issued_at,
    )
    data = {
        "intent_sha256": transfer.intent.sha256(),
        "consumption": None if fault == "pending" else receipt.model_dump(mode="json"),
        "checked_at": (
            datetime.now(UTC)
            + timedelta(seconds=60 if fault == "future" else -60 if fault == "stale" else 0)
        ).isoformat(),
    }
    if fault == "foreign":
        data["intent_sha256"] = "a" * 64
    calls = []
    api = None
    service = None
    if fault == "authenticated":
        from fastapi.testclient import TestClient

        from coire_api.app import create_app
        from coire_api.upgrades import expired_recovery_delivery
        from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryConsumptionState

        monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
        service = AsyncMock(
            return_value=UpgradeExpiredRecoveryConsumptionState.model_validate(data)
        )
        monkeypatch.setattr(expired_recovery_delivery, "read_consumption_state", service)

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

    def handler(request):
        calls.append(request)
        assert request.url.host == "core.private"
        assert request.url.path.endswith("/recovery-consumption-delivery")
        assert request.headers["X-Coire-Node"] == "coire-edge-a"
        if api is not None:
            import json

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
        if fault not in {None, "pending", "authenticated"}:
            with pytest.raises(UpgradeUnavailable):
                control.probe_recovery_consumption(transfer.intent, transfer.successor)
        else:
            state = control.probe_recovery_consumption(transfer.intent, transfer.successor)
            assert state.consumption == (None if fault == "pending" else receipt)
    assert len(calls) == 1
    if service is not None:
        service.assert_awaited_once()
        assert service.await_args.args[1:] == (
            transfer.intent.recovery_id,
            transfer.intent.sha256(),
            transfer.successor,
            "coire-edge-a",
        )
