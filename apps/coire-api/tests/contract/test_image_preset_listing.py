"""The image picker hides unavailable and explicit presets from ineligible callers."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import ApiKeyRow, get_session
from coire_api.images import presets
from coire_api.images.authorization import require_image_principal
from coire_api.routes import images
from coire_core.models.auth import UserRole
from coire_core.models.images import (
    ImageContentMode,
    ImagePreset,
    ImagePresetList,
    ImageSubmitRequest,
)
from coire_core.settings import Settings

OWNER = uuid.uuid4()
MODEL = uuid.uuid4()
KEY = uuid.uuid4()
IDS = [uuid.uuid4() for _ in range(3)]


class FakeRows:
    def __init__(self, entitlements: list[str], scopes: list[str] | None = None) -> None:
        self.entitlements = entitlements
        self.scopes = scopes or ["images"]
        self.queries = 0

    async def scalars(self, query: object) -> Any:
        self.queries += 1
        items: list[object] = (
            list(self.entitlements)
            if self.queries == 1
            else [SimpleNamespace(id=item, state="published") for item in IDS]
        )
        return SimpleNamespace(all=lambda: items)

    async def get(self, model: type[object], identity: object, **kwargs: object) -> Any:
        assert model is ApiKeyRow and identity == KEY
        return SimpleNamespace(scopes=self.scopes)


def _projection(
    preset_id: uuid.UUID, required: frozenset[str]
) -> tuple[ImagePreset, presets.PresetResolution]:
    explicit = "explicit" in required
    defaults = ImageSubmitRequest(
        model_id=MODEL,
        prompt="subject",
        content_mode=ImageContentMode.EXPLICIT if explicit else ImageContentMode.STANDARD,
    )
    return (
        ImagePreset(id=preset_id, revision=1, name=str(preset_id), defaults=defaults),
        presets.PresetResolution(
            request=defaults,
            preset_id=preset_id,
            preset_revision=1,
            dependency_ids=frozenset({MODEL}),
            required_entitlements=required,
        ),
    )


@pytest.mark.parametrize(
    "principal, entitlements, scopes, expected",
    [
        (
            Principal(kind=PrincipalKind.USER, user_id=OWNER, entitlements=frozenset({"explicit"})),
            [],
            None,
            {IDS[0]},
        ),
        (
            Principal(kind=PrincipalKind.ADMIN, user_id=OWNER, role=UserRole.ADMIN),
            [],
            None,
            {IDS[0]},
        ),
        (Principal(kind=PrincipalKind.USER, user_id=OWNER), ["explicit", "hidden"], None, set(IDS)),
        (
            Principal(
                kind=PrincipalKind.API_KEY,
                user_id=OWNER,
                api_key_id=KEY,
                credential_version=1,
                scopes=frozenset({"images"}),
            ),
            ["explicit", "hidden"],
            ["images"],
            {IDS[0], IDS[2]},
        ),
    ],
)
async def test_listing_filters_live_entitlements_and_key_scope(
    principal: Principal,
    entitlements: list[str],
    scopes: list[str] | None,
    expected: set[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeRows(entitlements, scopes)

    async def live(db: object, actor: Principal, **kwargs: object) -> uuid.UUID:
        return OWNER

    async def policy(
        db: object, request: ImageSubmitRequest
    ) -> tuple[ImagePreset, presets.PresetResolution]:
        assert request.preset_id is not None
        required = {
            IDS[0]: frozenset(),
            IDS[1]: frozenset({"explicit"}),
            IDS[2]: frozenset({"hidden"}),
        }[request.preset_id]
        return _projection(request.preset_id, required)

    monkeypatch.setattr(presets, "authorize_live_image_action", live)
    monkeypatch.setattr(presets, "_load_preset_policy", policy)
    listing = await presets.list_eligible_image_presets(cast(AsyncSession, session), principal)
    assert {item.id for item in listing.items} == expected


async def test_invalid_preset_is_absent_without_aborting_picker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_core.errors import ImageValidationError

    async def live(db: object, actor: Principal, **kwargs: object) -> uuid.UUID:
        return OWNER

    async def policy(
        db: object, request: ImageSubmitRequest
    ) -> tuple[ImagePreset, presets.PresetResolution]:
        if request.preset_id == IDS[1]:
            raise ImageValidationError()
        assert request.preset_id is not None
        return _projection(request.preset_id, frozenset())

    monkeypatch.setattr(presets, "authorize_live_image_action", live)
    monkeypatch.setattr(presets, "_load_preset_policy", policy)
    listing = await presets.list_eligible_image_presets(
        cast(AsyncSession, FakeRows([])), Principal(kind=PrincipalKind.USER, user_id=OWNER)
    )
    assert {item.id for item in listing.items} == {IDS[0], IDS[2]}


async def test_picker_route_is_disabled_until_image_admission_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = Principal(kind=PrincipalKind.USER, user_id=OWNER)
    app = FastAPI()
    app.state.settings = Settings(_secrets_dir="/nonexistent", image_enabled=False)  # type: ignore[call-arg]
    app.include_router(images.router)
    app.dependency_overrides[require_image_principal] = lambda: principal

    async def session():  # type: ignore[no-untyped-def]
        yield object()

    async def listing(db: object, actor: Principal) -> ImagePresetList:
        raise AssertionError("disabled picker must not read presets")

    app.dependency_overrides[get_session] = session
    monkeypatch.setattr(presets, "list_eligible_image_presets", listing)
    assert "get" in app.openapi()["paths"]["/api/v1/images/presets"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/images/presets")
    assert response.status_code == 200 and response.json() == {"items": []}

    async def enabled(db: object, actor: Principal) -> ImagePresetList:
        return ImagePresetList(items=[_projection(IDS[0], frozenset())[0]])

    app.state.settings.image_enabled = True
    monkeypatch.setattr(presets, "list_eligible_image_presets", enabled)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/images/presets")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [str(IDS[0])]
