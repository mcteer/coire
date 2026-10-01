"""Untrusted image recipes restore settings without reapplying preset text."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import ImageInputRow, ModelRow
from coire_api.images import metadata
from coire_core.errors import ImageForbidden, ImageValidationError
from coire_core.models.images import (
    ImageRecipe,
    ImageRecipeImportRequest,
    ImageSpec,
    ResolvedImageSpec,
    canonical_spec_hash,
)
from coire_core.models.registry import EngineBackend, ModelKind, ModelSource, ModelState, Visibility

OWNER = uuid.uuid4()
SOURCE = uuid.uuid4()
MODEL = uuid.uuid4()
PRESET = uuid.uuid4()


def _recipe() -> ImageRecipe:
    spec = ImageSpec(
        model_id=MODEL,
        prompt="preset preface, direct subject",
        width=512,
        height=512,
        steps=9,
        guidance=Decimal("0.125"),
        seed=7,
    )
    resolved = ResolvedImageSpec(
        spec=spec,
        seeds=(7,),
        pipeline_version="mflux-0.20.0",
        environment_fingerprint="a" * 64,
        model_sha256="b" * 64,
        preset_id=PRESET,
        preset_revision=3,
        spec_hash=canonical_spec_hash(spec),
    )
    return ImageRecipe(
        resolved=resolved,
        output_index=0,
        seed=7,
        pixel_sha256="c" * 64,
        width=512,
        height=512,
    )


async def test_import_replays_direct_effective_fields_without_preset_duplication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = ImageInputRow(
        id=SOURCE,
        owner_user_id=OWNER,
        purpose="recipe",
        state="ready",
        recipe=_recipe().model_dump(mode="json"),
        created_at=datetime.now(UTC),
    )
    model = ModelRow(
        id=MODEL,
        kind=ModelKind.IMAGE_MODEL,
        backend=EngineBackend.MFLUX,
        source=ModelSource.STUDIO,
        state=ModelState.READY,
        visibility=Visibility.PUBLISHED,
        manifest_sha256="b" * 64,
        entitlement=[],
    )

    async def owned(session: object, input_id: uuid.UUID, principal: Principal) -> ImageInputRow:
        assert input_id == SOURCE and principal.user_id == OWNER
        return source

    class Session:
        async def get(self, model_type: type[object], identity: object) -> object | None:
            assert model_type is ModelRow and identity == MODEL
            return model

        async def scalars(self, statement: object) -> object:
            return SimpleNamespace(all=lambda: [])

    monkeypatch.setattr(metadata, "require_owned_image_input", owned)

    async def authorized(*args: object, **kwargs: object) -> uuid.UUID:
        return OWNER

    monkeypatch.setattr(metadata, "authorize_live_image_action", authorized)
    actor = Principal(kind=PrincipalKind.USER, user_id=OWNER)
    request = ImageRecipeImportRequest()
    first = await metadata.import_image_recipe(
        cast(AsyncSession, Session()), actor, SOURCE, request
    )
    replay = await metadata.import_image_recipe(
        cast(AsyncSession, Session()), actor, SOURCE, request
    )
    assert first == replay
    assert first.settings.prompt == "preset preface, direct subject"
    assert first.settings.preset_id is None and first.settings.preset_revision is None
    assert first.settings.guidance == Decimal("0.125")
    assert first.settings.seed == 7
    assert first.missing_dependency_sha256 == []
    assert first.exact_reproduction_available is False
    assert first.unavailable_reason == "runtime_environment_changed"
    changed_runtime = _recipe().model_copy(
        update={
            "resolved": _recipe().resolved.model_copy(update={"pipeline_version": "mflux-0.19.0"})
        }
    )
    source.recipe = changed_runtime.model_dump(mode="json")
    runtime_result = await metadata.import_image_recipe(
        cast(AsyncSession, Session()), actor, SOURCE, request
    )
    assert runtime_result.unavailable_reason == "runtime_version_changed"
    model.manifest_sha256 = "d" * 64
    missing_model = await metadata.import_image_recipe(
        cast(AsyncSession, Session()), actor, SOURCE, request
    )
    assert missing_model.unavailable_reason == "model_components_unavailable"
    model.manifest_sha256 = "b" * 64
    model.entitlement = ["private-model"]

    async def refused(*args: object, **kwargs: object) -> uuid.UUID:
        raise ImageForbidden()

    monkeypatch.setattr(metadata, "authorize_live_image_action", refused)
    unauthorized_model = await metadata.import_image_recipe(
        cast(AsyncSession, Session()), actor, SOURCE, request
    )
    assert unauthorized_model.missing_dependency_sha256 == ["b" * 64]
    assert unauthorized_model.unavailable_reason == "model_components_unavailable"


def test_import_request_rejects_unknown_version_and_invalid_digest() -> None:
    with pytest.raises(ValidationError):
        ImageRecipeImportRequest.model_validate({"schema_version": 2})
    with pytest.raises(ValidationError):
        ImageRecipeImportRequest.model_validate({"replacement_inputs": {"not-a-digest": SOURCE}})


async def test_import_rejects_unbound_replacement_before_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = ImageInputRow(
        id=SOURCE,
        owner_user_id=OWNER,
        purpose="recipe",
        state="ready",
        recipe=_recipe().model_dump(mode="json"),
        created_at=datetime.now(UTC),
    )

    async def owned(session: object, input_id: uuid.UUID, principal: Principal) -> ImageInputRow:
        return source

    monkeypatch.setattr(metadata, "require_owned_image_input", owned)
    actor = Principal(kind=PrincipalKind.USER, user_id=OWNER)
    with pytest.raises(ImageValidationError, match="replacement is unknown"):
        await metadata.import_image_recipe(
            cast(AsyncSession, object()),
            actor,
            SOURCE,
            ImageRecipeImportRequest(replacement_inputs={"d" * 64: uuid.uuid4()}),
        )
