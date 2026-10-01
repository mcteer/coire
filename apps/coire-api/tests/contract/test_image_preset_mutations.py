"""Admin preset mutations append revisions and audit without prompt content."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, cast

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import ImagePresetRevisionRow, ImagePresetRow, ModelRow
from coire_api.images import admin_presets
from coire_core.errors import ImageConflict, ImageValidationError
from coire_core.models.images import (
    ImageContentMode,
    ImageLora,
    ImagePresetCreate,
    ImagePresetUpdate,
    ImageSubmitRequest,
)
from coire_core.models.registry import EngineBackend, ModelKind, ModelState

ADMIN = uuid.uuid4()
BASE = uuid.uuid4()
LORA = uuid.uuid4()
HIDDEN = uuid.uuid4()

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
    "required_dependency_ids": [str(HIDDEN)],
}


class FakeSession:
    def __init__(self, *, lora_kind: ModelKind = ModelKind.IMAGE_LORA) -> None:
        self.models: dict[uuid.UUID, ModelRow] = {
            BASE: ModelRow(
                id=BASE,
                kind=ModelKind.IMAGE_MODEL,
                backend=EngineBackend.MFLUX,
                source="studio",
                state=ModelState.READY,
                entitlement=["base"],
                image_capability_profile=PROFILE,
            ),
            LORA: ModelRow(
                id=LORA,
                kind=lora_kind,
                backend=EngineBackend.AUXILIARY,
                source="studio",
                state=ModelState.READY,
                entitlement=["adapter"],
            ),
            HIDDEN: ModelRow(
                id=HIDDEN,
                kind=ModelKind.IMAGE_CLASSIFIER,
                backend=EngineBackend.AUXILIARY,
                source="studio",
                state=ModelState.READY,
                entitlement=["hidden"],
            ),
        }
        self.pointer: ImagePresetRow | None = None
        self.revisions: dict[int, ImagePresetRevisionRow] = {}
        self.added: list[object] = []

    async def get(self, model: type[object], identity: object, **kwargs: object) -> Any:
        assert kwargs.get("with_for_update") is True
        if model is ModelRow:
            return self.models.get(identity) if isinstance(identity, uuid.UUID) else None
        if model is ImagePresetRow:
            return (
                self.pointer if self.pointer is not None and self.pointer.id == identity else None
            )
        if model is ImagePresetRevisionRow:
            return self.revisions.get(identity[1]) if isinstance(identity, tuple) else None
        raise AssertionError(model)

    def add(self, row: object) -> None:
        self.added.append(row)
        if isinstance(row, ImagePresetRow):
            self.pointer = row
        elif isinstance(row, ImagePresetRevisionRow):
            self.revisions[row.revision] = row

    async def flush(self) -> None:
        pass


def _create() -> ImagePresetCreate:
    return ImagePresetCreate(
        name="Portrait",
        prompt_prefix="studio",
        defaults=ImageSubmitRequest(
            model_id=BASE,
            prompt="subject",
            loras=[ImageLora(model_id=LORA, scale=Decimal("0.5"))],
            content_mode=ImageContentMode.EXPLICIT,
        ),
    )


async def test_create_freezes_hidden_dependencies_and_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audits: list[dict[str, object]] = []

    async def audit(session: object, **kwargs: object) -> None:
        audits.append(kwargs)

    monkeypatch.setattr(admin_presets, "write_audit", audit)
    session = FakeSession()
    result = await admin_presets.create_image_preset(
        cast(AsyncSession, session), _create(), admin_user_id=ADMIN, actor="admin:test"
    )
    assert result.revision == 1
    assert session.pointer is not None and session.pointer.state == "published"
    revision = session.revisions[1]
    assert set(revision.dependency_ids) == {str(LORA), str(HIDDEN)}
    assert set(revision.entitlement_requirements) == {"base", "adapter", "hidden", "explicit"}
    assert len(audits) == 1 and audits[0]["action"] == "image.preset.create"
    assert "subject" not in str(audits)


async def test_update_appends_revision_and_rejects_stale_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audits: list[dict[str, object]] = []

    async def audit(session: object, **kwargs: object) -> None:
        audits.append(kwargs)

    monkeypatch.setattr(admin_presets, "write_audit", audit)
    session = FakeSession()
    created = await admin_presets.create_image_preset(
        cast(AsyncSession, session), _create(), admin_user_id=ADMIN, actor="admin:test"
    )
    old = session.revisions[1]
    updated = await admin_presets.update_image_preset(
        cast(AsyncSession, session),
        created.id,
        ImagePresetUpdate(expected_revision=1, prompt_prefix="new studio"),
        admin_user_id=ADMIN,
        actor="admin:test",
    )
    assert updated.revision == 2 and updated.prompt_prefix == "new studio"
    assert old.prefix == "studio" and session.revisions[2] is not old
    with pytest.raises(ImageConflict):
        await admin_presets.update_image_preset(
            cast(AsyncSession, session),
            created.id,
            ImagePresetUpdate(expected_revision=1, name="stale"),
            admin_user_id=ADMIN,
            actor="admin:test",
        )
    assert [item["action"] for item in audits] == ["image.preset.create", "image.preset.update"]


async def test_retire_blocks_new_mutations_and_keeps_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audits: list[dict[str, object]] = []

    async def audit(session: object, **kwargs: object) -> None:
        audits.append(kwargs)

    monkeypatch.setattr(admin_presets, "write_audit", audit)
    session = FakeSession()
    created = await admin_presets.create_image_preset(
        cast(AsyncSession, session), _create(), admin_user_id=ADMIN, actor="admin:test"
    )
    await admin_presets.retire_image_preset(
        cast(AsyncSession, session), created.id, admin_user_id=ADMIN, actor="admin:test"
    )
    assert session.pointer is not None and session.pointer.state == "retired"
    assert 1 in session.revisions
    with pytest.raises(ImageConflict):
        await admin_presets.update_image_preset(
            cast(AsyncSession, session),
            created.id,
            ImagePresetUpdate(expected_revision=1, name="again"),
            admin_user_id=ADMIN,
            actor="admin:test",
        )
    assert [item["action"] for item in audits] == ["image.preset.create", "image.preset.retire"]


async def test_wrong_auxiliary_kind_cannot_be_published() -> None:
    session = FakeSession(lora_kind=ModelKind.CONTROL_MODEL)
    with pytest.raises(ImageValidationError):
        await admin_presets.create_image_preset(
            cast(AsyncSession, session), _create(), admin_user_id=ADMIN, actor="admin:test"
        )
    assert session.added == []


async def test_nested_preset_and_overlong_effective_prompt_are_rejected() -> None:
    session = FakeSession()
    with pytest.raises(ValidationError):
        ImagePresetCreate(
            name="Nested",
            defaults=ImageSubmitRequest(model_id=BASE, preset_id=uuid.uuid4(), prompt="subject"),
        )
    too_long = ImagePresetCreate(
        name="Long",
        prompt_prefix="p" * 1000,
        defaults=ImageSubmitRequest(model_id=BASE, prompt="s" * 4000),
    )
    with pytest.raises(ImageValidationError):
        await admin_presets.create_image_preset(
            cast(AsyncSession, session), too_long, admin_user_id=ADMIN, actor="admin:test"
        )
    assert session.added == []
