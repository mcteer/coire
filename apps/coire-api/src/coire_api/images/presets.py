"""Pure, fail-closed resolution of immutable image preset revisions."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import ValidationError

from coire_core.errors import ImageConflict, ImageValidationError
from coire_core.models.images import ImageContentMode, ImagePreset, ImageSubmitRequest


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
