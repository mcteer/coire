"""Pure, fail-closed resolution of immutable image preset revisions."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import ImagePresetRevisionRow, ImagePresetRow, ModelRow
from coire_api.images.authorization import authorize_live_image_action
from coire_core.errors import ImageConflict, ImageNotFound, ImageValidationError
from coire_core.models.images import (
    ImageCapabilityProfile,
    ImageContentMode,
    ImagePreset,
    ImageSubmitRequest,
)
from coire_core.models.registry import (
    AUXILIARY_IMAGE_KINDS,
    EngineBackend,
    ModelKind,
    ModelSource,
    ModelState,
)


@dataclass(frozen=True)
class PresetResolution:
    """Submission fields and immutable policy to recheck before admission."""

    request: ImageSubmitRequest
    preset_id: uuid.UUID
    preset_revision: int
    dependency_ids: frozenset[uuid.UUID]
    required_entitlements: frozenset[str]


def _prefixed_prompt(prefix: str, prompt: str | None) -> str | None:
    if not prefix or prompt is None:
        return prompt
    if prompt == prefix or prompt.startswith(f"{prefix} "):
        return prompt
    return f"{prefix} {prompt}"


def resolve_image_preset(
    request: ImageSubmitRequest,
    preset: ImagePreset,
    *,
    published: bool,
    preset_dependency_ids: frozenset[uuid.UUID],
    preset_requirements: frozenset[str],
    registry_requirements: Mapping[uuid.UUID, frozenset[str]],
) -> PresetResolution:
    """Overlay user fields while retaining preset model and dependency policy.

    The caller reads the current registry requirements and rechecks the returned
    requirement set against live entitlements in its admission transaction.
    """
    if (
        request.preset_id != preset.id
        or (request.preset_revision is not None and request.preset_revision != preset.revision)
        or not published
        or preset.retired
    ):
        raise ImageConflict()
    model_id = preset.defaults.model_id
    if model_id is None or (request.model_id is not None and request.model_id != model_id):
        raise ImageConflict()
    if preset.defaults.preset_id is not None:
        raise ImageValidationError()

    values = preset.defaults.model_dump(mode="python", exclude={"preset_id", "preset_revision"})
    overrides = request.model_dump(
        mode="python", exclude_unset=True, exclude={"preset_id", "preset_revision", "model_id"}
    )
    values.update(overrides)
    values["model_id"] = model_id
    values["prompt"] = _prefixed_prompt(preset.prompt_prefix, values.get("prompt"))

    dependency_ids = {model_id, *preset_dependency_ids}
    dependency_ids.update(item["model_id"] for item in values.get("loras") or [])
    for field in ("control", "upscale"):
        auxiliary = values.get(field)
        if auxiliary is not None:
            dependency_ids.add(auxiliary["model_id"])
    if not dependency_ids <= registry_requirements.keys():
        raise ImageValidationError()
    required = set(preset_requirements)
    for dependency_id in dependency_ids:
        required.update(registry_requirements[dependency_id])
    if values.get("content_mode") == ImageContentMode.EXPLICIT or "explicit" in required:
        values["content_mode"] = ImageContentMode.EXPLICIT
        required.add("explicit")
    try:
        resolved = ImageSubmitRequest.model_validate(values)
    except ValidationError as exc:
        raise ImageValidationError() from exc
    return PresetResolution(
        request=resolved,
        preset_id=preset.id,
        preset_revision=preset.revision,
        dependency_ids=frozenset(dependency_ids),
        required_entitlements=frozenset(required),
    )


def _asset_ids(request: ImageSubmitRequest) -> set[uuid.UUID]:
    ids = {request.model_id} if request.model_id is not None else set()
    ids.update(item.model_id for item in request.loras or [])
    if request.control is not None:
        ids.add(request.control.model_id)
    if request.upscale is not None:
        ids.add(request.upscale.model_id)
    return ids


def _stored_uuid_set(value: object) -> frozenset[uuid.UUID]:
    if (
        not isinstance(value, list)
        or len(value) > 16
        or any(not isinstance(item, str) for item in value)
    ):
        raise ImageValidationError()
    try:
        return frozenset(uuid.UUID(item) for item in value)
    except (ValueError, TypeError) as exc:
        raise ImageValidationError() from exc


def _stored_requirements(value: object) -> frozenset[str]:
    if (
        not isinstance(value, list)
        or len(value) > 32
        or any(not isinstance(item, str) or not item or len(item) > 64 for item in value)
    ):
        raise ImageValidationError()
    return frozenset(value)


async def load_resolved_image_preset(
    session: AsyncSession, request: ImageSubmitRequest, principal: Principal
) -> PresetResolution:
    """Load immutable preset and live dependencies under one admission transaction."""
    if request.preset_id is None:
        raise ImageValidationError()
    row = await session.get(
        ImagePresetRow, request.preset_id, populate_existing=True, with_for_update=True
    )
    if row is None or row.state != "published":
        raise ImageNotFound()
    revision = await session.get(
        ImagePresetRevisionRow,
        (row.id, row.current_revision),
        populate_existing=True,
        with_for_update=True,
    )
    if revision is None:
        raise ImageValidationError()
    try:
        defaults = ImageSubmitRequest.model_validate(revision.defaults)
        preset = ImagePreset(
            id=row.id,
            revision=revision.revision,
            name=row.name,
            prompt_prefix=revision.prefix,
            defaults=defaults,
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise ImageValidationError() from exc
    if request.preset_revision is not None and request.preset_revision != revision.revision:
        raise ImageConflict()
    if request.model_id is not None and request.model_id != defaults.model_id:
        raise ImageConflict()
    frozen_ids = _stored_uuid_set(revision.dependency_ids)
    requirements = _stored_requirements(revision.entitlement_requirements)
    base_id = defaults.model_id
    if base_id is None:
        raise ImageValidationError()
    base = await session.get(ModelRow, base_id, populate_existing=True, with_for_update=True)
    if (
        base is None
        or base.kind != ModelKind.IMAGE_MODEL
        or base.backend != EngineBackend.MFLUX
        or base.state is not ModelState.READY
        or base.source != ModelSource.STUDIO
        or base.image_capability_profile is None
    ):
        raise ImageValidationError()
    try:
        profile = ImageCapabilityProfile.model_validate(base.image_capability_profile)
    except ValidationError as exc:
        raise ImageValidationError() from exc
    all_frozen_ids = frozen_ids | frozenset(profile.required_dependency_ids)
    asset_ids = all_frozen_ids | _asset_ids(defaults) | _asset_ids(request)
    if len(asset_ids) > 16:
        raise ImageValidationError()

    models: dict[uuid.UUID, ModelRow] = {}
    registry_requirements: dict[uuid.UUID, frozenset[str]] = {}
    for model_id in sorted(asset_ids):
        model = (
            base
            if model_id == base_id
            else await session.get(ModelRow, model_id, populate_existing=True, with_for_update=True)
        )
        if (
            model is None
            or model.state is not ModelState.READY
            or model.source != ModelSource.STUDIO
        ):
            raise ImageValidationError()
        if model.kind == ModelKind.IMAGE_MODEL:
            if model.backend != EngineBackend.MFLUX or model.image_capability_profile is None:
                raise ImageValidationError()
            try:
                ImageCapabilityProfile.model_validate(model.image_capability_profile)
            except ValidationError as exc:
                raise ImageValidationError() from exc
        elif model.kind not in AUXILIARY_IMAGE_KINDS or model.backend != EngineBackend.AUXILIARY:
            raise ImageValidationError()
        models[model_id] = model
        registry_requirements[model_id] = _stored_requirements(model.entitlement or [])

    if models[base_id].kind != ModelKind.IMAGE_MODEL:
        raise ImageValidationError()
    if any(
        models[item].kind not in AUXILIARY_IMAGE_KINDS for item in profile.required_dependency_ids
    ):
        raise ImageValidationError()
    for source in (defaults, request):
        for lora in source.loras or []:
            if models[lora.model_id].kind != ModelKind.IMAGE_LORA:
                raise ImageValidationError()
        if (
            source.control is not None
            and models[source.control.model_id].kind != ModelKind.CONTROL_MODEL
        ):
            raise ImageValidationError()
        if (
            source.upscale is not None
            and models[source.upscale.model_id].kind != ModelKind.UPSCALE_MODEL
        ):
            raise ImageValidationError()

    resolved = resolve_image_preset(
        request,
        preset,
        published=True,
        preset_dependency_ids=all_frozen_ids,
        preset_requirements=requirements,
        registry_requirements=registry_requirements,
    )
    await authorize_live_image_action(
        session,
        principal,
        explicit=resolved.request.content_mode is ImageContentMode.EXPLICIT,
        required_entitlements=resolved.required_entitlements,
    )
    return resolved
