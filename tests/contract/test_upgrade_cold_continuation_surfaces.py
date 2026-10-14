"""Continuation auth precedes malformed bodies; SDK never retries new budgets."""

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from coire_api.app import create_app
from coire_core.client import UpgradeClient
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery_continuation import UpgradeColdContinuationRead
from coire_core.settings import Settings

UNIT_DIRECTORY = str(Path(__file__).resolve().parents[1] / "unit")


@pytest.mark.parametrize("fault", [None, "lost", "foreign"])
async def test_sdk_host_hold_reconciliation_pins_terminal_digest_without_retry(fault):
    from datetime import UTC, datetime
    from uuid import uuid4

    from coire_core.models.upgrade_recovery import (
        UpgradeRecoveryHostHoldReceipt,
        UpgradeRecoveryHostHoldRequest,
    )

    recovery_id = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
    body = UpgradeRecoveryHostHoldRequest(resolution_sha256="a" * 64)
    calls = []
    receipt = UpgradeRecoveryHostHoldReceipt(
        recovery_id=recovery_id,
        resolution_sha256="b" * 64 if fault == "foreign" else body.resolution_sha256,
        original_job_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        original_generation=57,
        plan_sha256="c" * 64,
        reconciled_by=uuid4(),
        reconciled_at=datetime.now(UTC),
    )

    def handle(request):
        calls.append(request)
        assert request.url.path == "/api/v1/admin/upgrades/recoveries/" + recovery_id + "/host-hold"
        if fault == "lost":
            raise httpx.ReadError("lost reply", request=request)
        return httpx.Response(200, json=receipt.model_dump(mode="json"))

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
        UpgradeClient("http://gateway.test", SecretStr("fixture-admin"), client=http) as client,
    ):
        if fault is not None:
            with pytest.raises(UpgradeUnavailable if fault == "lost" else UpgradeConflict):
                await client.reconcile_recovery_host_hold(recovery_id, body)
        else:
            assert await client.reconcile_recovery_host_hold(recovery_id, body) == receipt
    assert len(calls) == 1


@pytest.mark.parametrize(
    "route",
    [
        "/api/v1/admin/upgrades/recoveries/continuations",
        "/api/v1/admin/upgrades/recoveries/01ARZ3NDEKTSV4RRFFQ69G5FAW/host-hold",
        "/api/v1/admin/upgrades/recoveries/01ARZ3NDEKTSV4RRFFQ69G5FAW/continuation-schema",
        "/api/v1/admin/upgrades/recoveries/01ARZ3NDEKTSV4RRFFQ69G5FAW/continuation-schema-observation",
        "/api/v1/internal/upgrades/01ARZ3NDEKTSV4RRFFQ69G5FAW/recovery-cold-continuation-context",
        "/api/v1/internal/upgrades/01ARZ3NDEKTSV4RRFFQ69G5FAW/recovery-cold-continuation-observation",
        "/api/v1/internal/upgrades/01ARZ3NDEKTSV4RRFFQ69G5FAW/recovery-cold-continuation-binding",
    ],
)
def test_continuation_requires_credentials_before_body_or_database(monkeypatch, route):
    monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
    app = create_app(
        Settings(upgrades_enabled=True, node_tokens=SecretStr('{"coire-edge-a":"fixture-token"}'))
    )
    response = TestClient(app).post(route, json={})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("fault", [None, "lost", "foreign"])
async def test_sdk_continuation_acknowledges_exact_request_once(tmp_path, monkeypatch, fault):
    monkeypatch.syspath_prepend(UNIT_DIRECTORY)
    from test_upgrade_recovery_cold_continuation_contracts import grant_fixture

    request, grant = grant_fixture(tmp_path, monkeypatch)
    calls = []

    def handle(outgoing):
        calls.append(outgoing)
        assert outgoing.url.path == "/api/v1/admin/upgrades/recoveries/continuations"
        assert outgoing.method == "POST"
        if fault == "lost":
            raise httpx.ReadError("fixture lost reply", request=outgoing)
        intent = (
            grant.intent.model_copy(update={"recovery_id": request.previous_recovery_id})
            if fault == "foreign"
            else grant.intent
        )
        return httpx.Response(202, json=intent.model_dump(mode="json"))

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False) as http,
        UpgradeClient("http://gateway.test", SecretStr("fixture-admin"), client=http) as client,
    ):
        if fault:
            with pytest.raises(UpgradeUnavailable if fault == "lost" else UpgradeConflict):
                await client.continue_cold_recovery(request)
        else:
            assert await client.continue_cold_recovery(request) == grant.intent
    assert len(calls) == 1


async def test_sdk_continuation_schema_rejects_route_escape_before_transport():
    def forbidden(_request):
        raise AssertionError("invalid route reached transport")

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(forbidden), trust_env=False) as http,
        UpgradeClient("http://gateway.test", SecretStr("fixture-admin"), client=http) as client,
    ):
        with pytest.raises(ValidationError):
            await client.cold_continuation_schema(
                "../recoveries",
                UpgradeColdContinuationRead(
                    previous_intent_sha256="a" * 64, previous_owner_sha256="b" * 64
                ),
            )
