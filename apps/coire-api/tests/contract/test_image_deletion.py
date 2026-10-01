"""Delete route commits an owner tombstone under the current credential guard."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from coire_api.auth import Principal, PrincipalKind, require_principal
from coire_api.db import ImageOutputRow, UserRow, get_session
from coire_api.images import authorization
from coire_api.routes import image_outputs
from coire_core.errors import CoireError
from coire_core.settings import Settings

OWNER = uuid.uuid4()
ORIGIN = "https://coire.test"


class FakeSession:
    def __init__(self) -> None:
        self.output = ImageOutputRow(
            id=uuid.uuid4(),
            job_id="01K00000000000000000000000",
            owner_user_id=OWNER,
            output_index=0,
            blob_key="outputs/one.png",
            size_bytes=7,
            file_sha256="a" * 64,
            pixel_sha256="b" * 64,
            recipe={},
            content_tag="normal",
            classifier_provenance={},
            entitlement_snapshot={},
            state="published",
            created_at=datetime.now(UTC),
        )
        self.commits = 0

    async def get(self, model: type[object], identity: object, **_: object) -> object | None:
        if model is UserRow:
            return SimpleNamespace(active=True)
        if model is ImageOutputRow:
            return self.output if self.output.id == identity else None
        raise AssertionError(model)

    async def commit(self) -> None:
        self.commits += 1


def _app(principal: Principal, session: FakeSession, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    app = FastAPI()
    app.state.settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent", chat_browser_origin=ORIGIN
    )
    app.include_router(image_outputs.router)
    app.dependency_overrides[require_principal] = lambda: principal

    async def fake_get_session() -> Any:
        yield session

    app.dependency_overrides[get_session] = fake_get_session

    @asynccontextmanager
    async def fake_scope() -> Any:
        yield session

    monkeypatch.setattr(authorization, "session_scope", fake_scope)

    @app.exception_handler(CoireError)
    async def problem(request: Request, exc: CoireError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content=exc.to_problem().model_dump(mode="json")
        )

    return app


@pytest.mark.asyncio
async def test_delete_requires_owner_origin_and_hides_tombstone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    path = f"/api/v1/image-outputs/{session.output.id}"
    app = _app(Principal(kind=PrincipalKind.USER, user_id=OWNER), session, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        denied = await client.delete(path)
        accepted = await client.delete(path, headers={"Origin": ORIGIN})
        repeated = await client.delete(path, headers={"Origin": ORIGIN})
        hidden = await client.get(path)
    assert denied.status_code == 403 and session.commits == 2
    assert accepted.status_code == repeated.status_code == 202
    assert (
        accepted.json()
        == repeated.json()
        == {"output_id": str(session.output.id), "state": "tombstoned"}
    )
    assert hidden.status_code == 404


@pytest.mark.asyncio
async def test_other_user_cannot_tombstone_output(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    app = _app(Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4()), session, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        denied = await client.delete(
            f"/api/v1/image-outputs/{session.output.id}", headers={"Origin": ORIGIN}
        )
    assert denied.status_code == 404
    assert session.output.deleted_at is None and session.commits == 0
