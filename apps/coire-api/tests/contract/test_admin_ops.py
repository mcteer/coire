from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from coire_api.app import create_app
from coire_api.auth import Principal, PrincipalKind
from coire_api.db import get_session
from coire_core.models.auth import UserRole
from coire_core.settings import Settings


def _document() -> dict[str, Any]:
    app = create_app(Settings(_secrets_dir="/nonexistent"))  # type: ignore[call-arg]
    return app.openapi()


def test_ops_routes_are_present_with_exact_human_and_service_boundaries() -> None:
    paths = _document()["paths"]
    expected = {
        ("/api/v1/admin/ops/conversations", "post"),
        ("/api/v1/admin/ops/conversations/{conversation_id}", "get"),
        ("/api/v1/admin/ops/conversations/{conversation_id}/messages", "post"),
        ("/api/v1/admin/ops/proposals/{proposal_id}", "get"),
        ("/api/v1/admin/ops/proposals/{proposal_id}/confirm", "post"),
        ("/api/v1/admin/ops/proposals/{proposal_id}/decline", "post"),
        ("/api/v1/internal/ops/sessions", "post"),
        ("/api/v1/internal/ops/sessions/{session_id}", "patch"),
        ("/api/v1/internal/ops/proposals", "post"),
    }
    assert expected <= {(path, method) for path, item in paths.items() for method in item}
    for path, method in expected:
        assert paths[path][method]["security"] == [{"HTTPBearer": []}]


def test_confirmation_contract_requires_the_token_and_echoed_exact_action() -> None:
    document = _document()
    schema = document["components"]["schemas"]["OpsConfirmRequest"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"confirm_token", "action"}
    assert schema["properties"]["confirm_token"]["pattern"].startswith("^coire_confirm_")
    action = schema["properties"]["action"]
    assert action["discriminator"]["propertyName"] == "operation"
    assert set(action["discriminator"]["mapping"]) == {
        "instance.unload",
        "run.kill",
        "model.pin",
        "model.unpin",
        "instance.load",
    }


def test_irreversible_operations_are_absent_from_generated_openapi() -> None:
    encoded = str(_document())
    for forbidden in (
        "model.retire",
        "model.acquire",
        "user.delete",
        "shell.exec",
        "route.call",
    ):
        assert forbidden not in encoded


def test_create_message_confirm_and_decline_contracts_are_strict() -> None:
    document = _document()
    paths = document["paths"]
    assert set(paths["/api/v1/admin/ops/conversations"]["post"]["responses"]) >= {
        "201",
        "422",
    }
    assert set(
        paths["/api/v1/admin/ops/conversations/{conversation_id}/messages"]["post"][
            "responses"
        ]
    ) >= {"200", "422"}
    assert set(
        paths["/api/v1/admin/ops/proposals/{proposal_id}/confirm"]["post"]["responses"]
    ) >= {"202", "422"}
    assert set(
        paths["/api/v1/admin/ops/proposals/{proposal_id}/decline"]["post"]["responses"]
    ) >= {"200", "422"}
    decline = document["components"]["schemas"]["OpsDeclineRequest"]
    assert decline["additionalProperties"] is False


@pytest.mark.parametrize(
    "principal",
    [
        Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4(), role=UserRole.USER),
        Principal(
            kind=PrincipalKind.API_KEY,
            user_id=uuid.uuid4(),
            role=UserRole.USER,
            scopes=frozenset({"inference"}),
        ),
        Principal(
            kind=PrincipalKind.OPS_SERVICE,
            subject="coire-ops",
            scopes=frozenset({"ops:read", "ops:propose", "ops:session"}),
        ),
    ],
    ids=["human-non-admin", "api-key", "ops-service"],
)
async def test_only_a_human_admin_can_reach_confirmation(
    principal: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    @asynccontextmanager
    async def no_audit():  # type: ignore[no-untyped-def]
        yield None

    async def no_session():  # type: ignore[no-untyped-def]
        raise AssertionError("confirmation refusal must precede route database access")

    monkeypatch.setattr("coire_api.db.session_scope", no_audit)
    async def authenticate(_request: object) -> Principal:
        return principal

    monkeypatch.setattr("coire_api.auth.authenticate_request", authenticate)
    application = create_app(Settings(_secrets_dir="/nonexistent"))  # type: ignore[call-arg]
    application.dependency_overrides[get_session] = no_session
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/admin/ops/proposals/{uuid.uuid4()}/confirm",
            json={},
        )
    assert response.status_code == 403
    assert "coire_confirm_" not in response.text


def test_checked_in_openapi_is_fresh_for_ops_routes() -> None:
    checked_in = json.loads(
        (Path(__file__).resolve().parents[2] / "openapi.json").read_text()
    )
    assert checked_in == _document()
