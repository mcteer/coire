"""Stored presets recheck registry facts and live entitlements before admission."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import ImagePresetRevisionRow, ImagePresetRow, ModelRow
from coire_api.images import presets
from coire_core.errors import ImageConflict, ImageNotFound, ImageValidationError
from coire_core.models.images import ImageContentMode, ImageSubmitRequest
from coire_core.models.registry import EngineBackend, ModelKind, ModelState

OWNER = uuid.uuid4()
PRESET = uuid.uuid4()
BASE = uuid.uuid4()
ADAPTER = uuid.uuid4()

PROFILE: dict[str, object] = {
    "modes": ["txt2img"],
    "min_width": 64,
    "max_width": 1024,
    "min_height": 64,
    "max_height": 1024,
    "max_pixels": 1_048_576,
    "min_steps": 1,
    "max_steps": 50,
    "min_guidance": "0",
    "max_guidance": "10",
    "max_outputs": 4,
    "max_loras": 1,
}


def _rows() -> dict[tuple[type[object], object], object]:
    preset = ImagePresetRow(
        id=PRESET, name="Portrait", current_revision=2, state="published", created_by_user_id=OWNER
    )
    revision = ImagePresetRevisionRow(
        preset_id=PRESET,
        revision=2,
        defaults=ImageSubmitRequest(
            model_id=BASE,
            prompt="subject",
            content_mode=ImageContentMode.EXPLICIT,
        ).model_dump(mode="json"),
        prefix="studio",
        dependency_ids=[str(ADAPTER)],
        entitlement_requirements=["explicit"],
        created_by_user_id=OWNER,
    )
    base = ModelRow(
        id=BASE,
        kind=ModelKind.IMAGE_MODEL,
        backend=EngineBackend.MFLUX,
        source="studio",
        state=ModelState.READY,
        entitlement=["base"],
        image_capability_profile=PROFILE,
    )
    adapter = ModelRow(
        id=ADAPTER,
        kind=ModelKind.IMAGE_LORA,
        backend=EngineBackend.AUXILIARY,
        source="studio",
        state=ModelState.READY,
        entitlement=["adapter"],
    )
    return {
        (ImagePresetRow, PRESET): preset,
        (ImagePresetRevisionRow, (PRESET, 2)): revision,
        (ModelRow, BASE): base,
        (ModelRow, ADAPTER): adapter,
    }


class FakeSession:
    def __init__(self, rows: dict[tuple[type[object], object], object]) -> None:
        self.rows = rows

    async def get(self, model: type[object], identity: object, **kwargs: object) -> Any:
        assert kwargs.get("with_for_update") is True
        return self.rows.get((model, identity))


async def test_preset_store_unions_frozen_and_registry_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks: list[dict[str, object]] = []

    async def live(session: object, principal: Principal, **kwargs: object) -> uuid.UUID:
        checks.append(kwargs)
        return OWNER

    monkeypatch.setattr(presets, "authorize_live_image_action", live)
    principal = Principal(kind=PrincipalKind.USER, user_id=OWNER)
    result = await presets.load_resolved_image_preset(
        cast(AsyncSession, FakeSession(_rows())),
        ImageSubmitRequest(preset_id=PRESET, prompt="new subject", guidance=Decimal("2.25")),
        principal,
    )
    assert result.request.prompt == "studio new subject"
    assert result.required_entitlements == frozenset({"explicit", "base", "adapter"})
    assert checks == [
        {"explicit": True, "required_entitlements": frozenset({"explicit", "base", "adapter"})}
    ]


@pytest.mark.parametrize(
    "change, error",
    [
        (lambda rows: rows.pop((ImagePresetRow, PRESET)), ImageNotFound),
        (lambda rows: setattr(rows[(ImagePresetRow, PRESET)], "state", "retired"), ImageNotFound),
        (lambda rows: rows.pop((ModelRow, ADAPTER)), ImageValidationError),
        (
            lambda rows: setattr(rows[(ModelRow, BASE)], "kind", ModelKind.LANGUAGE_MODEL),
            ImageValidationError,
        ),
        (
            lambda rows: setattr(rows[(ModelRow, BASE)], "state", ModelState.RETIRED),
            ImageValidationError,
        ),
        (
            lambda rows: setattr(rows[(ModelRow, BASE)], "image_capability_profile", None),
            ImageValidationError,
        ),
        (
            lambda rows: setattr(
                rows[(ImagePresetRevisionRow, (PRESET, 2))],
                "defaults",
                {"model_id": str(BASE), "admin": True},
            ),
            ImageValidationError,
        ),
        (
            lambda rows: setattr(
                rows[(ImagePresetRevisionRow, (PRESET, 2))], "dependency_ids", ["not-a-uuid"]
            ),
            ImageValidationError,
        ),
        (
            lambda rows: setattr(
                rows[(ImagePresetRevisionRow, (PRESET, 2))], "dependency_ids", [123]
            ),
            ImageValidationError,
        ),
    ],
)
async def test_preset_store_fails_closed(
    change: Callable[[dict[tuple[type[object], object], object]], object],
    error: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _rows()
    change(rows)

    async def forbidden_live(session: object, principal: Principal, **kwargs: object) -> uuid.UUID:
        raise AssertionError("invalid preset must fail before live admission")

    monkeypatch.setattr(presets, "authorize_live_image_action", forbidden_live)
    with pytest.raises(error):
        await presets.load_resolved_image_preset(
            cast(AsyncSession, FakeSession(rows)),
            ImageSubmitRequest(preset_id=PRESET),
            Principal(kind=PrincipalKind.USER, user_id=OWNER),
        )


@pytest.mark.parametrize(
    "submit",
    [
        ImageSubmitRequest(preset_id=PRESET, preset_revision=1),
        ImageSubmitRequest(preset_id=PRESET, model_id=uuid.uuid4()),
    ],
)
async def test_stale_revision_or_model_rebind_conflicts_before_live_check(
    submit: ImageSubmitRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forbidden_live(session: object, principal: Principal, **kwargs: object) -> uuid.UUID:
        raise AssertionError("stale preset must fail before live admission")

    monkeypatch.setattr(presets, "authorize_live_image_action", forbidden_live)
    with pytest.raises(ImageConflict):
        await presets.load_resolved_image_preset(
            cast(AsyncSession, FakeSession(_rows())),
            submit,
            Principal(kind=PrincipalKind.USER, user_id=OWNER),
        )
