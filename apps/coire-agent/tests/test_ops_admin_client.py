from __future__ import annotations

import uuid

import httpx
import pytest
from coire_ops.admin_client import AdminClient

from coire_core.models.ops import OpsSessionRegistration


@pytest.mark.asyncio
async def test_ops_client_registers_with_typed_internal_route() -> None:
    session_id = uuid.uuid4()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/internal/ops/sessions"
        assert request.headers["authorization"] == "Bearer coire_ops_secret"
        return httpx.Response(
            201,
            json={
                "id": str(session_id),
                "service_instance": "ops-1",
                "state": "active",
                "started_at": "2026-09-01T00:00:00Z",
                "last_seen_at": "2026-09-01T00:00:00Z",
                "ended_at": None,
            },
        )

    client = AdminClient(
        api_url="http://coire-api:8000",
        token="coire_ops_secret",
        transport=httpx.MockTransport(handler),
    )
    result = await client.register_session(
        OpsSessionRegistration(session_id=session_id, service_instance="ops-1")
    )
    assert result.id == session_id


def test_ops_client_has_no_confirmation_or_generic_request_authority() -> None:
    public = {name for name in dir(AdminClient) if not name.startswith("_")}
    assert public == {
        "heartbeat_session",
        "read_snapshot",
        "register_session",
        "submit_proposal",
    }
    assert not any("confirm" in name for name in public)
