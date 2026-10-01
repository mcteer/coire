"""Private gallery metadata is owner scoped and uses the shared output projection."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ClauseElement

from coire_api.auth import Principal, PrincipalKind, require_principal
from coire_api.db import ImageOutputRow, UserRow, get_session
from coire_api.images import authorization, outputs
from coire_api.routes import image_outputs
from coire_core.errors import CoireError, ImageNotFound
from coire_core.models.images import (
    ImageContentTag,
    ImageRecipe,
    ImageSpec,
    ResolvedImageSpec,
    canonical_spec_hash,
)
from coire_core.settings import Settings

OWNER = uuid.uuid4()
OTHER = uuid.uuid4()
NOW = datetime.now(UTC)


def _recipe() -> ImageRecipe:
    spec = ImageSpec(
        model_id=uuid.uuid4(),
        prompt="private prompt",
        width=512,
        height=512,
        steps=20,
        guidance=Decimal("3"),
        seed=4,
    )
    resolved = ResolvedImageSpec(
        spec=spec,
        seeds=(4,),
        pipeline_version="mflux-0.20.0",
        environment_fingerprint="a" * 64,
        model_sha256="b" * 64,
        spec_hash=canonical_spec_hash(spec),
    )
    return ImageRecipe(
        resolved=resolved, output_index=0, seed=4, pixel_sha256="c" * 64, width=512, height=512
    )


def _row(owner: uuid.UUID = OWNER, *, tag: str = "normal", age: int = 0) -> ImageOutputRow:
    return ImageOutputRow(
        id=uuid.uuid4(),
        owner_user_id=owner,
        job_id="01K00000000000000000000000",
        output_index=0,
        blob_key="private/never-in-response.png",
        size_bytes=100,
        file_sha256="d" * 64,
        pixel_sha256="c" * 64,
        recipe=_recipe().model_dump(mode="json"),
        content_tag=tag,
        classifier_provenance={
            "tag": "normal",
            "score": "0.1",
            "classifier_revision": "a" * 40,
            "processor_sha256": "b" * 64,
            "tagged_at": NOW.isoformat(),
        },
        entitlement_snapshot={},
        state="published",
        created_at=NOW - timedelta(seconds=age),
    )


class Rows:
    def __init__(self, items: list[ImageOutputRow]) -> None:
        self.items = items

    def all(self) -> list[ImageOutputRow]:
        return self.items


class FakeSession:
    def __init__(self, items: list[ImageOutputRow]) -> None:
        self.items = items
        self.queries: list[str] = []

    async def get(self, model: type[object], identity: object, **kwargs: object) -> object | None:
        if model is UserRow:
            return SimpleNamespace(active=True)
        return next((item for item in self.items if item.id == identity), None)

    async def scalars(self, query: object) -> Rows:
        compiled = str(
            cast(ClauseElement, query).compile(
                dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
                compile_kwargs={"literal_binds": True},
            )
        )
        self.queries.append(compiled)
        if "FROM image_outputs" in compiled and "content_mode" in compiled:
            return Rows(
                [
                    item
                    for item in self.items
                    if item.content_tag != "explicit"
                    and cast(dict[str, Any], item.recipe)["resolved"]["spec"]["content_mode"]
                    == "standard"
                ]
            )
        return Rows(self.items)


async def test_gallery_page_is_bounded_owner_scoped_and_stable() -> None:
    items = [_row(age=0), _row(age=1), _row(age=2)]
    session = FakeSession(items)
    principal = Principal(kind=PrincipalKind.USER, user_id=OWNER)
    page = await outputs.list_owned_outputs(cast(AsyncSession, session), principal, limit=2)
    assert [item.id for item in page.items] == [items[0].id, items[1].id]
    assert page.next_cursor is not None and len(page.next_cursor) < 100
    boundary_at, boundary_id = outputs._decode_cursor(page.next_cursor, OWNER)
    assert boundary_id == items[1].id
    assert boundary_at == items[1].created_at
    assert all("blob_key" not in item.model_dump() for item in page.items)
    gallery_query = next(query for query in session.queries if "FROM image_outputs" in query)
    assert "image_outputs.owner_user_id" in gallery_query
    assert "image_outputs.deleted_at IS NULL" in gallery_query
    assert "image_outputs.state" in gallery_query
    assert "ORDER BY image_outputs.created_at DESC, image_outputs.id DESC" in gallery_query
    await outputs.list_owned_outputs(
        cast(AsyncSession, session), principal, limit=2, tag=ImageContentTag.UNKNOWN
    )
    assert "image_outputs.content_tag" in session.queries[-1]
    assert "content_mode" in session.queries[-1]


async def test_invalid_or_other_owner_cursor_is_hidden() -> None:
    other = _row(OTHER)
    own = _row()
    principal = Principal(kind=PrincipalKind.ADMIN, user_id=OWNER)
    with pytest.raises(ImageNotFound):
        await outputs.list_owned_outputs(
            cast(AsyncSession, FakeSession([other])),
            principal,
            cursor=outputs._encode_cursor(other),
        )
    with pytest.raises(ImageNotFound):
        await outputs.list_owned_outputs(
            cast(AsyncSession, FakeSession([])), principal, cursor="not-base64"
        )
    # A cursor remains valid if its boundary row was deleted between pages.
    empty = await outputs.list_owned_outputs(
        cast(AsyncSession, FakeSession([])), principal, cursor=outputs._encode_cursor(own)
    )
    assert empty.items == []


def _app(principal: Principal, monkeypatch: pytest.MonkeyPatch, session: FakeSession) -> FastAPI:
    app = FastAPI()
    app.state.settings = Settings(
        _secrets_dir="/nonexistent", chat_browser_origin="https://coire.test"
    )  # type: ignore[call-arg]
    app.include_router(image_outputs.router)
    app.dependency_overrides[require_principal] = lambda: principal

    async def fake_get_session() -> Any:
        yield session

    app.dependency_overrides[get_session] = fake_get_session

    @asynccontextmanager
    async def fake_scope() -> Any:
        yield session

    async def fake_live(*args: object, **kwargs: object) -> uuid.UUID:
        return OWNER

    monkeypatch.setattr(authorization, "session_scope", fake_scope)
    monkeypatch.setattr(authorization, "authorize_live_image_action", fake_live)

    @app.exception_handler(CoireError)
    async def problem(request: Request, exc: CoireError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content=exc.to_problem().model_dump(mode="json")
        )

    return app


async def test_gallery_routes_return_private_recipe_and_hide_other_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    own = _row()
    other = _row(OTHER)
    session = FakeSession([own])
    app = _app(Principal(kind=PrincipalKind.USER, user_id=OWNER), monkeypatch, session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://coire.test"
    ) as client:
        listed = await client.get("/api/v1/image-outputs?limit=1")
        detail = await client.get(f"/api/v1/image-outputs/{own.id}")
        missing = await client.get(f"/api/v1/image-outputs/{other.id}")
        invalid_cursor = await client.get("/api/v1/image-outputs?cursor=not-base64")
    assert listed.status_code == 200
    assert detail.status_code == 200
    assert listed.headers["cache-control"] == "private, no-store"
    assert detail.headers["cache-control"] == "private, no-store"
    assert detail.json()["recipe"]["resolved"]["spec"]["prompt"] == "private prompt"
    assert "blob_key" not in str(detail.json())
    assert missing.status_code == 404
    assert invalid_cursor.status_code == 404


async def test_admin_cannot_read_another_users_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = _row(OTHER)
    app = _app(
        Principal(kind=PrincipalKind.ADMIN, user_id=OWNER),
        monkeypatch,
        FakeSession([other]),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://coire.test"
    ) as client:
        response = await client.get(f"/api/v1/image-outputs/{other.id}")
    assert response.status_code == 404


async def test_gallery_detail_refuses_explicit_recipe_without_live_entitlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit = _row(tag="explicit")
    cast(dict[str, Any], explicit.recipe)["resolved"]["spec"]["content_mode"] = "explicit"
    session = FakeSession([explicit])
    live_check = authorization.authorize_live_image_action
    app = _app(Principal(kind=PrincipalKind.USER, user_id=OWNER), monkeypatch, session)
    monkeypatch.setattr(authorization, "authorize_live_image_action", live_check)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://coire.test"
    ) as client:
        detail = await client.get(f"/api/v1/image-outputs/{explicit.id}")
        listed = await client.get("/api/v1/image-outputs")
    assert detail.status_code == 403, detail.text
    assert "content_mode" in session.queries[-1]
    assert listed.status_code == 200
    assert listed.json()["items"] == []
