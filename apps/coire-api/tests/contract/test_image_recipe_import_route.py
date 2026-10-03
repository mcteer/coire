"""Recipe import is an owner-scoped typed settings read, never a job submission."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import get_session
from coire_api.images import metadata
from coire_api.images.authorization import require_image_principal
from coire_api.routes import image_inputs
from coire_core.models.images import (
    ImageRecipe,
    ImageRecipeImport,
    ImageRecipeImportRequest,
    ImageSpec,
    ImageSubmitRequest,
    ResolvedImageSpec,
    canonical_spec_hash,
)

OWNER = uuid.uuid4()
INPUT = uuid.uuid4()
MODEL = uuid.uuid4()


async def test_recipe_import_route_is_typed_private_and_side_effect_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = ImageSpec(
        model_id=MODEL,
        prompt="private subject",
        width=512,
        height=512,
        steps=9,
        guidance=Decimal(0),
        seed=7,
    )
    recipe = ImageRecipe(
        resolved=ResolvedImageSpec(
            spec=spec,
            seeds=(7,),
            pipeline_version="mflux-0.20.0",
            environment_fingerprint="a" * 64,
            model_sha256="b" * 64,
            spec_hash=canonical_spec_hash(spec),
        ),
        output_index=0,
        seed=7,
        pixel_sha256="c" * 64,
        width=512,
        height=512,
    )
    principal = Principal(kind=PrincipalKind.USER, user_id=OWNER)
    app = FastAPI()
    app.include_router(image_inputs.router)
    app.dependency_overrides[require_image_principal] = lambda: principal

    async def session():  # type: ignore[no-untyped-def]
        yield object()

    app.dependency_overrides[get_session] = session

    async def imported(
        db: object, actor: Principal, input_id: uuid.UUID, request: ImageRecipeImportRequest
    ) -> ImageRecipeImport:
        assert input_id == INPUT and actor == principal and request.replacement_inputs == {}
        return ImageRecipeImport(
            recipe=recipe,
            settings=ImageSubmitRequest(model_id=MODEL, prompt=spec.prompt),
            exact_reproduction_available=False,
            unavailable_reason="runtime_environment_unverified",
        )

    monkeypatch.setattr(metadata, "import_image_recipe", imported)
    assert "post" in app.openapi()["paths"]["/api/v1/image-inputs/{input_id}/recipe"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/v1/image-inputs/{INPUT}/recipe", json={})
        invalid = await client.post(f"/api/v1/image-inputs/{INPUT}/recipe", json={"admin": True})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["settings"]["prompt"] == "private subject"
    assert response.json()["exact_reproduction_available"] is False
    assert invalid.status_code == 422
