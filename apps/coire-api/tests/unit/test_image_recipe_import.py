"""An owned PNG recipe restores settings without trusting its IDs or runtime claim."""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import ImageInputRow, ModelRow
from coire_api.images import metadata
from coire_api.images.metadata import import_image_recipe
from coire_core.errors import ImageConflict, ImageValidationError
from coire_core.models.images import (
    ImageInputDigest,
    ImageMode,
    ImageRecipe,
    ImageRecipeImportRequest,
    ImageSpec,
    ResolvedImageSpec,
    canonical_spec_hash,
)
from coire_core.models.registry import EngineBackend, ModelKind, ModelSource, ModelState, Visibility
from coire_scheduler.image_admission import image_environment_fingerprint

OWNER = uuid.uuid4()
RECIPE_ID = uuid.uuid4()
MODEL = uuid.uuid4()
ORIGINAL = uuid.uuid4()
REPLACEMENT = uuid.uuid4()
PRINCIPAL = Principal(kind=PrincipalKind.USER, user_id=OWNER)


NODE = SimpleNamespace(
    name="coire-edge-b", memory_total_bytes=64 * 1024**3, gpu_cores=32, agent_version="1.0"
)


def _recipe(*, with_input: bool = False, matching_environment: bool = False) -> ImageRecipe:
    spec = ImageSpec(
        model_id=MODEL,
        mode=ImageMode.IMG2IMG if with_input else ImageMode.TXT2IMG,
        prompt="prefix subject",
        width=512,
        height=512,
        steps=9,
        guidance=Decimal("0.0000"),
        seed=7,
        init_image_id=ORIGINAL if with_input else None,
        strength=Decimal("0.625") if with_input else None,
    )
    resolved = ResolvedImageSpec(
        spec=spec,
        seeds=(7,),
        pipeline_version="mflux-0.20.0",
        environment_fingerprint=(
            image_environment_fingerprint(cast(Any, NODE), "b" * 64)
            if matching_environment
            else "a" * 64
        ),
        model_sha256="b" * 64,
        inputs=(
            (ImageInputDigest(input_id=ORIGINAL, sha256="c" * 64, width=512, height=512),)
            if with_input
            else ()
        ),
        spec_hash=canonical_spec_hash(spec),
    )
    return ImageRecipe(
        resolved=resolved,
        output_index=0,
        seed=7,
        pixel_sha256="d" * 64,
        width=512,
        height=512,
    )


class Session:
    def __init__(self, recipe: ImageRecipe) -> None:
        self.source = SimpleNamespace(
            id=RECIPE_ID,
            owner_user_id=OWNER,
            purpose="recipe",
            state="ready",
            deleted_at=None,
            recipe=recipe.model_dump(mode="json"),
        )
        self.base = SimpleNamespace(
            kind=ModelKind.IMAGE_MODEL,
            backend=EngineBackend.MFLUX,
            source=ModelSource.STUDIO,
            state=ModelState.READY,
            visibility=Visibility.PUBLISHED,
            manifest_sha256="b" * 64,
            entitlement=[],
        )
        self.inputs: dict[uuid.UUID, Any] = {}

    async def scalars(self, statement: object) -> Any:
        return SimpleNamespace(all=lambda: [NODE])

    async def get(self, model: type[object], identity: object) -> Any:
        if model is ImageInputRow:
            return (
                self.source if identity == RECIPE_ID else self.inputs.get(cast(uuid.UUID, identity))
            )
        assert model is ModelRow and identity == MODEL
        return self.base


@pytest.fixture(autouse=True)
def live_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    async def authorized(*args: object, **kwargs: object) -> uuid.UUID:
        return OWNER

    monkeypatch.setattr(metadata, "authorize_live_image_action", authorized)


async def test_recipe_restores_exact_direct_fields_without_reapplying_prefix() -> None:
    session = Session(_recipe())
    result = await import_image_recipe(
        cast(AsyncSession, session), PRINCIPAL, RECIPE_ID, ImageRecipeImportRequest()
    )
    assert result.settings.prompt == "prefix subject"
    assert result.settings.guidance == Decimal("0.0000")
    assert result.settings.preset_id is None
    assert result.settings.seed == 7
    assert result.missing_dependency_sha256 == []
    assert not result.exact_reproduction_available
    assert result.unavailable_reason == "runtime_environment_changed"


async def test_matching_declared_node_still_requires_pixel_equivalence_gate() -> None:
    session = Session(_recipe(matching_environment=True))
    result = await import_image_recipe(
        cast(AsyncSession, session), PRINCIPAL, RECIPE_ID, ImageRecipeImportRequest()
    )
    assert result.unavailable_reason == "runtime_environment_unverified"
    assert not result.exact_reproduction_available


async def test_recipe_rebinds_matching_owned_input_and_reports_missing_one() -> None:
    session = Session(_recipe(with_input=True))
    missing = await import_image_recipe(
        cast(AsyncSession, session), PRINCIPAL, RECIPE_ID, ImageRecipeImportRequest()
    )
    assert missing.missing_input_sha256 == ["c" * 64]
    session.inputs[REPLACEMENT] = SimpleNamespace(
        owner_user_id=OWNER,
        purpose="init",
        state="ready",
        deleted_at=None,
        normalized_sha256="c" * 64,
        normalized_width=512,
        normalized_height=512,
    )
    restored = await import_image_recipe(
        cast(AsyncSession, session),
        PRINCIPAL,
        RECIPE_ID,
        ImageRecipeImportRequest(replacement_inputs={"c" * 64: REPLACEMENT}),
    )
    assert restored.settings.init_image_id == REPLACEMENT
    assert restored.settings.strength == Decimal("0.625")
    assert restored.missing_input_sha256 == []


async def test_recipe_refuses_forged_replacement_or_nonrecipe_source() -> None:
    session = Session(_recipe(with_input=True))
    with pytest.raises(ImageValidationError):
        await import_image_recipe(
            cast(AsyncSession, session),
            PRINCIPAL,
            RECIPE_ID,
            ImageRecipeImportRequest(replacement_inputs={"e" * 64: REPLACEMENT}),
        )
    session.inputs[REPLACEMENT] = SimpleNamespace(
        owner_user_id=uuid.uuid4(),
        purpose="init",
        state="ready",
        deleted_at=None,
        normalized_sha256="c" * 64,
        normalized_width=512,
        normalized_height=512,
    )
    with pytest.raises(ImageValidationError):
        await import_image_recipe(
            cast(AsyncSession, session),
            PRINCIPAL,
            RECIPE_ID,
            ImageRecipeImportRequest(replacement_inputs={"c" * 64: REPLACEMENT}),
        )
    session.source.purpose = "init"
    with pytest.raises(ImageConflict):
        await import_image_recipe(
            cast(AsyncSession, session), PRINCIPAL, RECIPE_ID, ImageRecipeImportRequest()
        )


async def test_recipe_replacement_must_have_original_input_purpose() -> None:
    session = Session(_recipe(with_input=True))
    session.inputs[REPLACEMENT] = SimpleNamespace(
        owner_user_id=OWNER,
        purpose="mask",
        state="ready",
        deleted_at=None,
        normalized_sha256="c" * 64,
        normalized_width=512,
        normalized_height=512,
    )
    with pytest.raises(ImageValidationError, match="replacement is unavailable"):
        await import_image_recipe(
            cast(AsyncSession, session),
            PRINCIPAL,
            RECIPE_ID,
            ImageRecipeImportRequest(replacement_inputs={"c" * 64: REPLACEMENT}),
        )
