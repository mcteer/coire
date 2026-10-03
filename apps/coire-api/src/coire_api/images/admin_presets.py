"""Append-only image preset mutation service for future human-admin routes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.db import ImagePresetRevisionRow, ImagePresetRow, ModelRow
from coire_core.errors import ImageConflict, ImageNotFound, ImageValidationError
from coire_core.models.audit import AuditOutcome
from coire_core.models.auth import ActorType
from coire_core.models.images import (
    ImageCapabilityProfile,
    ImageContentMode,
    ImagePreset,
    ImagePresetCreate,
    ImagePresetUpdate,
    ImageSubmitRequest,
)
from coire_core.models.registry import (
    AUXILIARY_IMAGE_KINDS,
    EngineBackend,
    ModelKind,
    ModelSource,
    ModelState,
)


async def _policy_for_defaults(
    session: AsyncSession, defaults: ImageSubmitRequest
) -> tuple[list[str], list[str]]:
    if defaults.preset_id is not None or defaults.preset_revision is not None:
        raise ImageValidationError()
    base_id = defaults.model_id
    if base_id is None:
        raise ImageValidationError()
    base = await session.get(ModelRow, base_id, populate_existing=True, with_for_update=True)
    if (
        base is None
        or base.kind != ModelKind.IMAGE_MODEL
        or base.backend != EngineBackend.MFLUX
        or base.source != ModelSource.STUDIO
        or base.state is not ModelState.READY
        or base.image_capability_profile is None
    ):
        raise ImageValidationError()
    try:
        profile = ImageCapabilityProfile.model_validate(base.image_capability_profile)
    except ValidationError as exc:
        raise ImageValidationError() from exc

    required = set(_entitlement_names(base.entitlement))
    if defaults.content_mode is ImageContentMode.EXPLICIT:
        required.add("explicit")
    expected: dict[uuid.UUID, ModelKind | None] = dict.fromkeys(profile.required_dependency_ids)
    for lora in defaults.loras or []:
        expected[lora.model_id] = ModelKind.IMAGE_LORA
    if defaults.control is not None:
        expected[defaults.control.model_id] = ModelKind.CONTROL_MODEL
    if defaults.upscale is not None:
        expected[defaults.upscale.model_id] = ModelKind.UPSCALE_MODEL
    if base_id in expected or len(expected) > 16:
        raise ImageValidationError()

    for model_id, kind in sorted(expected.items()):
        model = await session.get(ModelRow, model_id, populate_existing=True, with_for_update=True)
        if (
            model is None
            or model.kind not in AUXILIARY_IMAGE_KINDS
            or (kind is not None and model.kind != kind)
            or model.backend != EngineBackend.AUXILIARY
            or model.source != ModelSource.STUDIO
            or model.state is not ModelState.READY
        ):
            raise ImageValidationError()
        required.update(_entitlement_names(model.entitlement))
    return [str(item) for item in sorted(expected)], sorted(required)


def _entitlement_names(value: object) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > 32
        or any(not isinstance(item, str) or not item or len(item) > 64 for item in value)
    ):
        raise ImageValidationError()
    return value


def _check_prefix(prefix: str, defaults: ImageSubmitRequest) -> None:
    prompt = defaults.prompt
    if prefix and prompt:
        effective = (
            prompt if prompt == prefix or prompt.startswith(f"{prefix} ") else f"{prefix} {prompt}"
        )
        if len(effective) > 4000:
            raise ImageValidationError()


def _project(row: ImagePresetRow, revision: ImagePresetRevisionRow) -> ImagePreset:
    try:
        return ImagePreset(
            id=row.id,
            revision=revision.revision,
            name=row.name,
            prompt_prefix=revision.prefix,
            defaults=ImageSubmitRequest.model_validate(revision.defaults),
            retired=row.state == "retired",
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise ImageValidationError() from exc


async def _audit(
    session: AsyncSession,
    *,
    actor: str,
    admin_user_id: uuid.UUID,
    action: str,
    preset_id: uuid.UUID,
    revision: int,
) -> None:
    await write_audit(
        session,
        actor=actor,
        actor_type=ActorType.USER,
        actor_user_id=admin_user_id,
        action=action,
        target_type="image_preset",
        target_id=str(preset_id),
        outcome=AuditOutcome.OK,
        detail={"revision": revision},
    )


async def create_image_preset(
    session: AsyncSession,
    request: ImagePresetCreate,
    *,
    admin_user_id: uuid.UUID,
    actor: str,
) -> ImagePreset:
    """Publish revision one; caller commits the pointer, revision and audit together."""
    _check_prefix(request.prompt_prefix, request.defaults)
    dependencies, requirements = await _policy_for_defaults(session, request.defaults)
    preset_id = uuid.uuid4()
    row = ImagePresetRow(
        id=preset_id,
        name=request.name,
        current_revision=1,
        state="published",
        created_by_user_id=admin_user_id,
    )
    revision = ImagePresetRevisionRow(
        preset_id=preset_id,
        revision=1,
        defaults=request.defaults.model_dump(mode="json"),
        prefix=request.prompt_prefix,
        dependency_ids=dependencies,
        entitlement_requirements=requirements,
        created_by_user_id=admin_user_id,
    )
    session.add(row)
    session.add(revision)
    await session.flush()
    await _audit(
        session,
        actor=actor,
        admin_user_id=admin_user_id,
        action="image.preset.create",
        preset_id=preset_id,
        revision=1,
    )
    return _project(row, revision)


async def update_image_preset(
    session: AsyncSession,
    preset_id: uuid.UUID,
    request: ImagePresetUpdate,
    *,
    admin_user_id: uuid.UUID,
    actor: str,
) -> ImagePreset:
    """Advance the locked pointer by inserting a new immutable revision."""
    row = await session.get(ImagePresetRow, preset_id, populate_existing=True, with_for_update=True)
    if row is None:
        raise ImageNotFound()
    if row.state != "published" or row.current_revision != request.expected_revision:
        raise ImageConflict()
    previous = await session.get(
        ImagePresetRevisionRow,
        (preset_id, row.current_revision),
        populate_existing=True,
        with_for_update=True,
    )
    if previous is None:
        raise ImageValidationError()
    try:
        defaults = (
            request.defaults
            if request.defaults is not None
            else ImageSubmitRequest.model_validate(previous.defaults)
        )
    except ValidationError as exc:
        raise ImageValidationError() from exc
    prefix = request.prompt_prefix if request.prompt_prefix is not None else previous.prefix
    _check_prefix(prefix, defaults)
    dependencies, requirements = await _policy_for_defaults(session, defaults)
    row.name = request.name if request.name is not None else row.name
    row.current_revision += 1
    row.updated_at = datetime.now(UTC)
    revision = ImagePresetRevisionRow(
        preset_id=preset_id,
        revision=row.current_revision,
        defaults=defaults.model_dump(mode="json"),
        prefix=prefix,
        dependency_ids=dependencies,
        entitlement_requirements=requirements,
        created_by_user_id=admin_user_id,
    )
    session.add(revision)
    await session.flush()
    await _audit(
        session,
        actor=actor,
        admin_user_id=admin_user_id,
        action="image.preset.update",
        preset_id=preset_id,
        revision=revision.revision,
    )
    return _project(row, revision)


async def retire_image_preset(
    session: AsyncSession,
    preset_id: uuid.UUID,
    *,
    admin_user_id: uuid.UUID,
    actor: str,
) -> None:
    """Retire only the mutable pointer; jobs keep their revision foreign key."""
    row = await session.get(ImagePresetRow, preset_id, populate_existing=True, with_for_update=True)
    if row is None:
        raise ImageNotFound()
    if row.state != "published":
        raise ImageConflict()
    row.state = "retired"
    row.updated_at = datetime.now(UTC)
    await session.flush()
    await _audit(
        session,
        actor=actor,
        admin_user_id=admin_user_id,
        action="image.preset.retire",
        preset_id=preset_id,
        revision=row.current_revision,
    )
