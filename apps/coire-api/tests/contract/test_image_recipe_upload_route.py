"""Recipe upload route requires current image owner authority and bounded metadata."""

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
from coire_api.db import ImageInputRow, UserRow, get_session
from coire_api.images import authorization, input_deletion, inputs
from coire_api.routes import image_inputs
from coire_core.errors import CoireError
from coire_core.models.images import ImageInput, ImageInputUpload
from coire_core.settings import Settings

OWNER = uuid.uuid4()
ORIGIN = "https://coire.test"


class FakeSession:
    async def commit(self) -> None:
        pass

    async def get(self, model: type[object], identity: object, **_: object) -> object | None:
        assert model is UserRow
        return SimpleNamespace(active=True)


def _app(monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True) -> FastAPI:
    app = FastAPI()
    app.state.settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent", chat_browser_origin=ORIGIN, image_enabled=enabled
    )
    app.include_router(image_inputs.router)
    app.dependency_overrides[require_principal] = lambda: Principal(
        kind=PrincipalKind.USER, user_id=OWNER
    )
    session = FakeSession()

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


async def test_recipe_upload_requires_origin_and_returns_private_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []

    async def fake_admit(*args: object) -> ImageInput:
        metadata = args[2]
        assert isinstance(metadata, ImageInputUpload)
        called.append("admitted")
        return ImageInput(
            id=uuid.uuid4(),
            purpose=metadata.purpose,
            state="processing",
            byte_count=7,
            sha256="a" * 64,
            created_at=datetime.now(UTC),
        )

    monkeypatch.setattr(inputs, "admit_image_input_upload", fake_admit)
    app = _app(monkeypatch)
    form = {"purpose": "recipe", "filename": "source.png", "byte_count": "7"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        denied = await client.post("/api/v1/image-inputs", data=form, files={"file": b"pngdata"})
        accepted = await client.post(
            "/api/v1/image-inputs",
            data=form,
            files={"file": b"pngdata"},
            headers={"Origin": ORIGIN},
        )
        generation = await client.post(
            "/api/v1/image-inputs",
            data={**form, "purpose": "init"},
            files={"file": b"pngdata"},
            headers={"Origin": ORIGIN},
        )
    assert denied.status_code == 403
    assert accepted.status_code == 202 and accepted.json()["state"] == "processing"
    assert accepted.headers["cache-control"] == "private, no-store"
    assert generation.status_code == 202 and generation.json()["purpose"] == "init"
    assert called == ["admitted", "admitted"]


async def test_recipe_upload_stays_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(monkeypatch, enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.post(
            "/api/v1/image-inputs",
            data={"purpose": "recipe", "filename": "source.png", "byte_count": "7"},
            files={"file": b"pngdata"},
            headers={"Origin": ORIGIN},
        )
    assert response.status_code == 403


async def test_owner_can_poll_existing_input_when_admission_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_id = uuid.uuid4()
    row = ImageInputRow(
        id=input_id,
        owner_user_id=OWNER,
        purpose="recipe",
        original_key=str(input_id),
        original_bytes=7,
        original_sha256="a" * 64,
        state="processing",
        held_bytes=7,
        active_references=0,
        created_at=datetime.now(UTC),
    )

    async def owned(session: object, requested: uuid.UUID, principal: Principal) -> ImageInputRow:
        assert requested == input_id and principal.user_id == OWNER
        return row

    monkeypatch.setattr(image_inputs, "require_owned_image_input", owned)
    app = _app(monkeypatch, enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.get(f"/api/v1/image-inputs/{input_id}")
    assert response.status_code == 200 and response.json()["state"] == "processing"


async def test_owner_delete_requires_origin_and_commits_tombstone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_id = uuid.uuid4()
    committed: list[str] = []

    async def tombstone(session: object, principal: Principal, requested: uuid.UUID) -> ImageInput:
        assert principal.user_id == OWNER and requested == input_id
        committed.append("tombstone")
        return ImageInput(
            id=input_id,
            purpose="recipe",
            state="deleting",
            byte_count=7,
            sha256="a" * 64,
            created_at=datetime.now(UTC),
        )

    async def commit(self: FakeSession) -> None:
        committed.append("commit")

    monkeypatch.setattr(input_deletion, "tombstone_owned_input", tombstone)
    monkeypatch.setattr(FakeSession, "commit", commit)
    app = _app(monkeypatch, enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        denied = await client.delete(f"/api/v1/image-inputs/{input_id}")
        accepted = await client.delete(
            f"/api/v1/image-inputs/{input_id}", headers={"Origin": ORIGIN}
        )
    assert denied.status_code == 403
    assert accepted.status_code == 202 and accepted.json()["state"] == "deleting"
    assert accepted.headers["cache-control"] == "private, no-store"
    assert committed == ["tombstone", "commit"]
