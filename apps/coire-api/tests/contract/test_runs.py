from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr

from coire_api.app import create_app
from coire_api.auth import Principal, PrincipalKind, require_principal
from coire_api.db import AgentRunRow, get_session
from coire_api.runs import RunNotFound, get_visible_run
from coire_core.models.runs import AgentRunState
from coire_core.settings import Settings


def test_run_routes_are_typed_authenticated_and_strict() -> None:
    document = create_app(
        Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    ).openapi()
    paths = document["paths"]
    for path, methods in {
        "/api/v1/runs": ("get", "post"),
        "/api/v1/runs/{run_id}": ("get",),
        "/api/v1/runs/{run_id}/events": ("get",),
        "/api/v1/admin/runs": ("get",),
        "/api/v1/admin/runs/{run_id}": ("delete",),
    }.items():
        for method in methods:
            assert paths[path][method]["security"] == [{"HTTPBearer": []}]
    schemas = document["components"]["schemas"]
    assert schemas["AgentRunCreate"]["additionalProperties"] is False
    assert "image" not in schemas["AgentRunCreate"]["properties"]
    assert "argv" not in schemas["AgentRunCreate"]["properties"]


class Session:
    def __init__(self, row: AgentRunRow) -> None:
        self.row = row

    async def get(self, _: object, run_id: uuid.UUID) -> AgentRunRow | None:
        return self.row if run_id == self.row.id else None


async def test_owner_can_see_run_and_other_user_gets_uniform_not_found() -> None:
    owner = uuid.uuid4()
    row = AgentRunRow(
        id=uuid.uuid4(),
        requester_user_id=owner,
        profile="general",
        primary_model_id=uuid.uuid4(),
        primary_variant_id=uuid.uuid4(),
        workspace_ref="workspace",
        token_scope={},
        state=AgentRunState.QUEUED,
        limits={},
        resource_usage={},
    )
    session = Session(row)
    principal = Principal(kind=PrincipalKind.USER, subject=str(owner), user_id=owner)
    assert await get_visible_run(session, row.id, principal) is row  # type: ignore[arg-type]
    stranger = Principal(kind=PrincipalKind.USER, subject="stranger", user_id=uuid.uuid4())
    with pytest.raises(RunNotFound):
        await get_visible_run(session, row.id, stranger)  # type: ignore[arg-type]


async def test_missing_run_uses_stable_rfc9457_code() -> None:
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    settings.admin_token = SecretStr("test-admin")
    settings.identity_legacy_admin_enabled = True
    app = create_app(settings)
    user_id = uuid.uuid4()

    class Missing:
        async def get(self, *_: object) -> None:
            return None

    async def session() -> AsyncIterator[object]:
        yield Missing()

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[require_principal] = lambda: Principal(
        kind=PrincipalKind.USER, subject=str(user_id), user_id=user_id
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/v1/runs/{uuid.uuid4()}",
            headers={"Authorization": "Bearer test-admin"},
        )
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == "urn:coire:problem:run_not_found"
