"""Typed recovery mutations never acquire another budget after lost transport."""

from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from coire_core.client import UpgradeClient
from coire_core.errors import UpgradeConflict, UpgradeUnavailable

UNIT_DIRECTORY = str(Path(__file__).resolve().parents[1] / "unit")


@pytest.mark.parametrize("fault", [None, "lost", "foreign"])
async def test_sdk_recovery_acceptance_is_exact_and_has_no_automatic_retry(
    tmp_path, monkeypatch, fault
):
    monkeypatch.syspath_prepend(UNIT_DIRECTORY)
    from test_upgrade_recovery_cold_owner import cold_fixture

    _, intent, *_ = cold_fixture(tmp_path)
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST" and request.url.path == "/api/v1/admin/upgrades/recoveries"
        if fault == "lost":
            raise httpx.ReadError("lost", request=request)
        response = intent
        if fault == "foreign":
            response = intent.model_copy(
                update={"request": intent.request.model_copy(update={"idempotency_key": "foreign"})}
            )
        return httpx.Response(202, json=response.model_dump(mode="json"))

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as http,
        UpgradeClient("http://gateway.test", SecretStr("fixture-key"), client=http) as client,
    ):
        if fault:
            with pytest.raises(UpgradeUnavailable if fault == "lost" else UpgradeConflict):
                await client.create_recovery(intent.request)
        else:
            assert await client.create_recovery(intent.request) == intent
    assert len(calls) == 1


@pytest.mark.parametrize("method", ["recovery_status", "recovery_schema"])
async def test_recovery_sdk_reads_reject_route_escape(method):
    def forbidden(request):
        raise AssertionError("Transport must not receive caller route escape")

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(forbidden), trust_env=False) as http,
        UpgradeClient("http://gateway.test", SecretStr("fixture-key"), client=http) as client,
    ):
        with pytest.raises(ValidationError):
            await getattr(client, method)("../releases")
