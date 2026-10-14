"""SDK uses only typed authenticated API requests and never retries a mutation."""

from unittest.mock import Mock

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from upgrade_fixtures import plan

from coire_core.client import UpgradeClient
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeNotFound, UpgradeUnavailable
from coire_core.models.upgrades import UpgradeStartRequest


@pytest.mark.parametrize(
    "fault", [None, "lost", "redirect", "forbidden", "missing", "conflict", "shape", "oversize"]
)
async def test_start_sdk_validates_response_and_preserves_idempotency(fault: str | None) -> None:
    value = plan()
    request_body = UpgradeStartRequest(
        release_id=value.target.release_id,
        expected_source_sha256=value.operation.source_sha256,
        idempotency_key=value.operation.idempotency_key,
        acknowledge_maintenance=True,
    )
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "POST"
        assert request.url.path == "/api/v1/admin/upgrades/nodes/coire-edge-a"
        assert request.headers["Authorization"] == "Bearer human-test-key"
        assert UpgradeStartRequest.model_validate_json(request.content) == request_body
        if fault == "lost":
            raise httpx.ReadError("private transport error", request=request)
        if fault in {"redirect", "forbidden", "missing", "conflict"}:
            return httpx.Response(
                {"redirect": 302, "forbidden": 403, "missing": 404, "conflict": 409}[fault]
            )
        if fault == "oversize":
            return httpx.Response(200, content=b"x" * (1024 * 1024 + 1))
        return httpx.Response(
            202, json={} if fault == "shape" else value.operation.model_dump(mode="json")
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False) as http:
        async with UpgradeClient(
            "http://gateway.test", SecretStr("human-test-key"), client=http
        ) as client:
            if fault is None:
                assert await client.start_studio("coire-edge-a", request_body) == value.operation
            else:
                error = {
                    "forbidden": UpgradeForbidden,
                    "missing": UpgradeNotFound,
                    "conflict": UpgradeConflict,
                    "shape": ValidationError,
                }.get(fault, UpgradeUnavailable)
                with pytest.raises(error):
                    await client.start_studio("coire-edge-a", request_body)
        assert not http.is_closed
    assert len(calls) == 1


@pytest.mark.parametrize("job_id", ["../releases", "job?token=private", "invalid"])
async def test_sdk_rejects_route_escape_before_transport(job_id: str) -> None:
    handler = Mock()
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as http,
        UpgradeClient("http://gateway.test", SecretStr("human-test-key"), client=http) as client,
    ):
        with pytest.raises(ValidationError):
            await client.status(job_id)
    handler.assert_not_called()
