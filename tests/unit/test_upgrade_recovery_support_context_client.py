"""Fresh fixed Core context never accepts recaptured or redirected intent."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr
from test_upgrade_expired_recovery_transfer import transfer_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerTransfer
from coire_core.settings import Settings


@pytest.mark.parametrize("fault", [None, "extra", "redirect", "authenticated_route"])
@pytest.mark.parametrize("recovery_port", [None, 48519])
def test_support_context_exact_captured_intent(tmp_path, monkeypatch, fault, recovery_port):
    from coire_node.upgrade_guard import NativeGuardTransport

    intent = UpgradeExpiredRecoveryOwnerTransfer.model_validate(transfer_payload(tmp_path)).intent
    calls = []
    api = None
    service = None
    if fault == "authenticated_route":
        from fastapi.testclient import TestClient

        from coire_api.app import create_app
        from coire_api.upgrades import expired_recovery_owner

        monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
        service = AsyncMock(return_value=intent)
        monkeypatch.setattr(expired_recovery_owner, "read_live_support_context", service)

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
        assert (
            request.url.path
            == f"/api/v1/internal/upgrades/{intent.recovery_id}/recovery-support-context"
        )
        assert request.url.host == "core.private"
        assert request.url.port == (recovery_port or settings.core_api_port)
        assert request.content == b'{"protocol":1}'
        assert request.headers["Authorization"] == "Bearer fixture-token"
        assert request.headers["X-Coire-Node"] == "coire-edge-a"
        if api is not None:
            response = api.post(
                request.url.path, json={"protocol": 1}, headers=dict(request.headers)
            )
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "no-store"
            return httpx.Response(response.status_code, json=response.json())
        data = intent.model_dump(mode="json")
        if fault == "extra":
            data["credential"] = "caller-value"
        return httpx.Response(
            302 if fault == "redirect" else 200,
            json=data,
            headers={"Location": "http://foreign"} if fault == "redirect" else {},
        )

    settings = Settings(
        upgrades_enabled=True,
        upgrade_recovery_core_api_port=recovery_port,
        node_name="coire-edge-a",
        node_control_host="127.0.0.1",
        core_control_host="core.private",
        node_token=SecretStr("fixture-token"),
    )
    with httpx.Client(
        transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False
    ) as client:
        control = NativeGuardTransport(settings, client)
        if fault in {"extra", "redirect"}:
            with pytest.raises(UpgradeUnavailable):
                control.verify_support_context(intent)
        else:
            assert control.verify_support_context(intent) == intent
    assert len(calls) == 1
    if service is not None:
        service.assert_awaited_once()
        assert service.await_args.args[1:] == (intent.recovery_id, "coire-edge-a")


@pytest.mark.parametrize("fields", [{"upgrades_enabled": False}, {"node_name": "coire-core"}])
def test_scoped_control_port_refuses_disabled_or_core_configuration(fields):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(
            **({"upgrades_enabled": True, "node_name": "coire-edge-a"} | fields),
            upgrade_recovery_core_api_port=48519,
        )
