"""The image model picker exposes only currently admissible published bases."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import ModelRow, get_session
from coire_api.images import catalog
from coire_api.images.authorization import require_image_principal
from coire_api.routes import images
from coire_core.errors import ImageForbidden
from coire_core.models.images import ImageCapabilityProfile, ImageModelList
from coire_core.models.registry import EngineBackend, ModelKind, ModelSource, ModelState, Visibility
from coire_core.settings import Settings

OWNER = uuid.uuid4()
BASE = uuid.uuid4()
EXPLICIT = uuid.uuid4()
BROKEN = uuid.uuid4()
HIDDEN = uuid.uuid4()


def _base(model_id: uuid.UUID, *, explicit: bool = False, hidden: bool = False) -> Any:
    profile = ImageCapabilityProfile.model_validate(
        {
            "modes": ["txt2img", "img2img"],
            "min_width": 256,
            "max_width": 1024,
            "min_height": 256,
            "max_height": 1024,
            "max_pixels": 1024 * 1024,
            "min_steps": 2,
            "max_steps": 30,
            "min_guidance": "0",
            "max_guidance": "4",
            "max_outputs": 2,
            "default_width": 512,
            "default_height": 512,
            "default_steps": 9,
            "default_guidance": "0",
            "required_dependency_ids": [HIDDEN] if hidden else [],
        }
    )
    return SimpleNamespace(
        id=model_id,
        slug=f"model-{model_id.hex}",
        display_name="Image model",
        kind=ModelKind.IMAGE_MODEL,
        backend=EngineBackend.MFLUX,
        source=ModelSource.STUDIO,
        state=ModelState.READY,
        visibility=Visibility.PUBLISHED,
        image_capability_profile=profile.model_dump(mode="json"),
        entitlement=["explicit"] if explicit else [],
        manifest_sha256="a" * 64,
    )


class Session:
    def __init__(self) -> None:
        self.rows = {
            BASE: _base(BASE),
            EXPLICIT: _base(EXPLICIT, explicit=True),
            BROKEN: _base(BROKEN, hidden=True),
        }
        self.rows[HIDDEN] = SimpleNamespace(
            id=HIDDEN,
            kind=ModelKind.IMAGE_LORA,
            backend=EngineBackend.AUXILIARY,
            source=ModelSource.STUDIO,
            state=ModelState.FAILED,
            manifest_sha256="b" * 64,
            entitlement=[],
        )

    async def scalars(self, query: object) -> Any:
        return SimpleNamespace(all=lambda: list(self.rows.values())[:3])

    async def get(self, model: type[object], identity: object, **kwargs: object) -> Any:
        assert model is ModelRow and kwargs.get("with_for_update") is True
        return self.rows.get(cast(uuid.UUID, identity))


@pytest.mark.parametrize(
    "scopes, entitlements, expected",
    [
        (frozenset({"images"}), frozenset(), {BASE}),
        (frozenset({"images"}), frozenset({"explicit"}), {BASE}),
        (frozenset({"images", "images:explicit"}), frozenset({"explicit"}), {BASE, EXPLICIT}),
    ],
)
async def test_model_picker_filters_hidden_dependencies_and_live_authority(
    scopes: frozenset[str],
    entitlements: frozenset[str],
    expected: set[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = Principal(
        kind=PrincipalKind.API_KEY,
        user_id=OWNER,
        api_key_id=uuid.uuid4(),
        credential_version=1,
        scopes=scopes,
    )

    async def live(
        session: object,
        actor: Principal,
        *,
        explicit: bool = False,
        required_entitlements: frozenset[str] = frozenset(),
    ) -> uuid.UUID:
        if explicit and "images:explicit" not in actor.scopes:
            raise ImageForbidden()
        if not required_entitlements <= entitlements:
            raise ImageForbidden()
        return OWNER

    monkeypatch.setattr(catalog, "authorize_live_image_action", live)
    listing = await catalog.list_eligible_image_models(cast(AsyncSession, Session()), principal)
    assert {item.id for item in listing.items} == expected
    assert all(item.capability.modes == ("txt2img", "img2img") for item in listing.items)
    assert all(not item.capability.required_dependency_ids for item in listing.items)
    assert all(item.capability.max_guidance == 0 for item in listing.items)
    assert all(item.capability.max_loras == 0 for item in listing.items)
    assert all(not item.capability.supports_negative_prompt for item in listing.items)
    assert all(item.residency == "unknown" for item in listing.items)


async def test_model_picker_never_advertises_unimplemented_native_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    session.rows[BASE].image_capability_profile["modes"] = ["txt2img", "fill", "control"]

    async def live(db: object, actor: Principal, **kwargs: object) -> uuid.UUID:
        return OWNER

    monkeypatch.setattr(catalog, "authorize_live_image_action", live)
    listing = await catalog.list_eligible_image_models(
        cast(AsyncSession, session), Principal(kind=PrincipalKind.USER, user_id=OWNER)
    )
    selected = next(item for item in listing.items if item.id == BASE)
    assert selected.capability.modes == ("txt2img",)


async def test_model_picker_omits_base_with_unsupported_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    session.rows[BASE].image_capability_profile["default_guidance"] = "1.5"

    async def live(db: object, actor: Principal, **kwargs: object) -> uuid.UUID:
        return OWNER

    monkeypatch.setattr(catalog, "authorize_live_image_action", live)
    result = await catalog.list_eligible_image_models(
        cast(AsyncSession, session), Principal(kind=PrincipalKind.USER, user_id=OWNER)
    )
    assert BASE not in {item.id for item in result.items}


async def test_model_picker_route_is_typed_and_disabled_until_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = Principal(kind=PrincipalKind.USER, user_id=OWNER)
    app = FastAPI()
    app.state.settings = Settings(_secrets_dir="/nonexistent", image_enabled=False)  # type: ignore[call-arg]
    app.include_router(images.router)
    app.dependency_overrides[require_image_principal] = lambda: principal

    async def session():  # type: ignore[no-untyped-def]
        yield object()

    app.dependency_overrides[get_session] = session

    async def listing(db: object, actor: Principal) -> ImageModelList:
        raise AssertionError("disabled picker must not query registry")

    monkeypatch.setattr(catalog, "list_eligible_image_models", listing)
    assert "get" in app.openapi()["paths"]["/api/v1/images/models"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/images/models")
    assert response.status_code == 200 and response.json() == {"items": []}
    assert response.headers["cache-control"] == "private, no-store"

    async def enabled(db: object, actor: Principal) -> ImageModelList:
        return ImageModelList(items=[])

    app.state.settings.image_enabled = True
    monkeypatch.setattr(catalog, "list_eligible_image_models", enabled)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/images/models")
    assert response.status_code == 200 and response.json() == {"items": []}
