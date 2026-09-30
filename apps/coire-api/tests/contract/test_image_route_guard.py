"""Image routes refuse and audit before service work; ordinary reads stay private."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind, require_principal
from coire_api.images import authorization
from coire_core.errors import CoireError, ImageForbidden, ImageNotFound
from coire_core.settings import Settings

OWNER = uuid.uuid4()
OTHER = uuid.uuid4()
ORIGIN = "https://coire.example.test"


def _app(
    principal: Principal, monkeypatch: pytest.MonkeyPatch, audits: list[dict[str, Any]]
) -> FastAPI:
    app = FastAPI()
    app.state.settings = Settings(_secrets_dir="/nonexistent", chat_browser_origin=ORIGIN)  # type: ignore[call-arg]
    app.dependency_overrides[require_principal] = lambda: principal

    @asynccontextmanager
    async def fake_scope() -> Any:
        yield object()

    async def fake_live(session: object, current: Principal, **kwargs: object) -> uuid.UUID:
        return OWNER

    async def fake_audit(session: object, **kwargs: object) -> None:
        audits.append(kwargs)

    monkeypatch.setattr(authorization, "session_scope", fake_scope)
    monkeypatch.setattr(authorization, "authorize_live_image_action", fake_live)
    monkeypatch.setattr(authorization, "write_audit", fake_audit)

    @app.exception_handler(CoireError)
    async def problem(request: Request, exc: CoireError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content=exc.to_problem().model_dump(mode="json")
        )

    @app.get("/images")
    async def read(user: authorization.CurrentImageUser) -> dict[str, str]:
        return {"user_id": str(user.user_id)}

    @app.post("/images")
    async def mutate(user: authorization.CurrentImageUser) -> dict[str, str]:
        return {"user_id": str(user.user_id)}

    return app


@pytest.mark.parametrize(
    "principal",
    [
        Principal(kind=PrincipalKind.SERVICE, user_id=OWNER),
        Principal(kind=PrincipalKind.OPS_SERVICE),
        Principal(kind=PrincipalKind.RUN, user_id=OWNER),
        Principal(kind=PrincipalKind.ADMIN),
        Principal(kind=PrincipalKind.API_KEY, user_id=OWNER, scopes=frozenset({"chat"})),
    ],
)
async def test_route_refusal_is_audited_without_content(
    principal: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    audits: list[dict[str, Any]] = []
    async with AsyncClient(
        transport=ASGITransport(app=_app(principal, monkeypatch, audits)),
        base_url=ORIGIN,
    ) as client:
        response = await client.get("/images")
    assert response.status_code == 403
    assert response.json()["coire_code"] == "image_forbidden"
    assert len(audits) == 1
    assert audits[0]["action"] == "image.refused"
    assert "prompt" not in str(audits[0]).lower()


async def test_browser_mutation_origin_and_live_recheck_are_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audits: list[dict[str, Any]] = []
    principal = Principal(kind=PrincipalKind.USER, user_id=OWNER)
    app = _app(principal, monkeypatch, audits)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        assert (await client.post("/images")).status_code == 403
        assert (await client.post("/images", headers={"Origin": ORIGIN})).status_code == 200
    assert len(audits) == 1
    assert audits[0]["target_id"] == "POST /images"


async def test_live_recheck_refusal_is_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    audits: list[dict[str, Any]] = []
    app = _app(Principal(kind=PrincipalKind.USER, user_id=OWNER), monkeypatch, audits)

    async def revoked(session: object, current: Principal, **kwargs: object) -> uuid.UUID:
        raise ImageForbidden()

    monkeypatch.setattr(authorization, "authorize_live_image_action", revoked)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.get("/images")
    assert response.status_code == 403
    assert len(audits) == 1
    assert audits[0]["action"] == "image.refused"


class FakeRows:
    def __init__(self, row: object | None) -> None:
        self.row = row

    async def get(self, model: type[object], identity: object) -> object | None:
        return self.row


async def test_ordinary_reads_hide_non_owner_and_unpublished_rows() -> None:
    admin = Principal(kind=PrincipalKind.ADMIN, user_id=OWNER)
    for row in (
        None,
        SimpleNamespace(owner_user_id=OTHER),
    ):
        with pytest.raises(ImageNotFound):
            await authorization.require_owned_image_job(
                cast(AsyncSession, FakeRows(row)), "job", admin
            )
    input_row = SimpleNamespace(owner_user_id=OWNER, deleted_at=object())
    with pytest.raises(ImageNotFound):
        await authorization.require_owned_image_input(
            cast(AsyncSession, FakeRows(input_row)), uuid.uuid4(), admin
        )
    output_row = SimpleNamespace(owner_user_id=OWNER, state="staged", deleted_at=None)
    with pytest.raises(ImageNotFound):
        await authorization.require_owned_image_output(
            cast(AsyncSession, FakeRows(output_row)), uuid.uuid4(), admin
        )
    own_job = SimpleNamespace(owner_user_id=OWNER)
    assert (
        await authorization.require_owned_image_job(
            cast(AsyncSession, FakeRows(own_job)), "job", admin
        )
    ).owner_user_id == OWNER
    published = SimpleNamespace(owner_user_id=OWNER, state="published", deleted_at=None)
    assert (
        await authorization.require_owned_image_output(
            cast(AsyncSession, FakeRows(published)), uuid.uuid4(), admin
        )
    ).state == "published"
