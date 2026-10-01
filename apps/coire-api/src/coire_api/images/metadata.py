"""Treat imported PNG recipes as untrusted settings, never as execution authority."""

from __future__ import annotations

import uuid

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import ImageInputRow, ModelRow, NodeRow
from coire_api.images.authorization import authorize_live_image_action, require_owned_image_input
from coire_core.errors import ImageConflict, ImageForbidden, ImageValidationError
from coire_core.models.images import (
    IMAGE_RUNTIME_VERSION,
    ImageContentMode,
    ImageInputDigest,
    ImageRecipe,
    ImageRecipeImport,
    ImageRecipeImportRequest,
    ImageSubmitRequest,
)
from coire_core.models.node import NodeRole, Reachability
from coire_core.models.registry import (
    AUXILIARY_IMAGE_KINDS,
    EngineBackend,
    ModelKind,
    ModelSource,
    ModelState,
    Visibility,
)
from coire_scheduler.image_admission import image_environment_fingerprint


def _source_purposes(recipe: ImageRecipe) -> dict[uuid.UUID, set[str]]:
    spec = recipe.resolved.spec
    purposes: dict[uuid.UUID, set[str]] = {}
    if spec.init_image_id is not None:
        purposes.setdefault(spec.init_image_id, set()).add("init")
    if spec.mask_id is not None:
        purposes.setdefault(spec.mask_id, set()).add("mask")
    if spec.control is not None:
        purposes.setdefault(spec.control.image_id, set()).add("control")
    return purposes


def _usable_input(
    row: ImageInputRow | None,
    owner_id: uuid.UUID,
    item: ImageInputDigest,
    expected_purposes: set[str],
) -> bool:
    return (
        row is not None
        and row.owner_user_id == owner_id
        and row.purpose in expected_purposes
        and len(expected_purposes) == 1
        and row.state == "ready"
        and row.deleted_at is None
        and row.normalized_sha256 == item.sha256
        and row.normalized_width == item.width
        and row.normalized_height == item.height
    )


async def _asset_authorized(
    session: AsyncSession, principal: Principal, asset: ModelRow, *, explicit: bool
) -> bool:
    entitlements = getattr(asset, "entitlement", None)
    if (
        not isinstance(entitlements, list)
        or len(entitlements) > 32
        or any(not isinstance(item, str) or not item or len(item) > 64 for item in entitlements)
    ):
        return False
    try:
        await authorize_live_image_action(
            session,
            principal,
            explicit=explicit or "explicit" in entitlements,
            required_entitlements=frozenset(entitlements),
        )
    except ImageForbidden:
        return False
    return True


async def import_image_recipe(
    session: AsyncSession,
    principal: Principal,
    input_id: uuid.UUID,
    request: ImageRecipeImportRequest,
) -> ImageRecipeImport:
    """Restore direct fields once; never acquire a model or claim an unverified runtime."""
    source = await require_owned_image_input(session, input_id, principal)
    if source.purpose != "recipe" or source.state != "ready" or source.recipe is None:
        raise ImageConflict("image recipe is not ready")
    try:
        recipe = ImageRecipe.model_validate(source.recipe)
    except ValidationError as exc:
        raise ImageConflict("image recipe is invalid") from exc
    resolved = recipe.resolved
    source_purposes = _source_purposes(recipe)
    if set(source_purposes) != {item.input_id for item in resolved.inputs}:
        raise ImageValidationError("image recipe input bindings differ")
    expected_digests = {item.sha256 for item in resolved.inputs}
    if not request.replacement_inputs.keys() <= expected_digests:
        raise ImageValidationError("image recipe replacement is unknown")

    assert principal.user_id is not None
    rebound: dict[uuid.UUID, uuid.UUID] = {}
    missing_inputs: list[str] = []
    for item in resolved.inputs:
        replacement = request.replacement_inputs.get(item.sha256)
        selected_id = replacement or item.input_id
        candidate = await session.get(ImageInputRow, selected_id)
        if _usable_input(candidate, principal.user_id, item, source_purposes[item.input_id]):
            rebound[item.input_id] = selected_id
        elif replacement is not None:
            raise ImageValidationError("image recipe replacement is unavailable")
        elif item.sha256 not in missing_inputs:
            missing_inputs.append(item.sha256)

    spec_values = resolved.spec.model_dump(mode="python")
    for field in ("init_image_id", "mask_id"):
        original = spec_values.get(field)
        if original in rebound:
            spec_values[field] = rebound[original]
    control = spec_values.get("control")
    if isinstance(control, dict) and control.get("image_id") in rebound:
        control["image_id"] = rebound[control["image_id"]]
    try:
        settings = ImageSubmitRequest.model_validate(spec_values)
    except ValidationError as exc:
        raise ImageValidationError("image recipe settings are invalid") from exc

    missing_dependencies: list[str] = []
    base = await session.get(ModelRow, resolved.spec.model_id)
    base_unavailable = (
        base is None
        or base.kind != ModelKind.IMAGE_MODEL
        or base.backend != EngineBackend.MFLUX
        or base.source != ModelSource.STUDIO
        or base.state != ModelState.READY
        or base.visibility != Visibility.PUBLISHED
        or base.manifest_sha256 != resolved.model_sha256
    )
    if not base_unavailable and base is not None:
        base_unavailable = not await _asset_authorized(
            session,
            principal,
            base,
            explicit=resolved.spec.content_mode is ImageContentMode.EXPLICIT,
        )
    if base_unavailable:
        missing_dependencies.append(resolved.model_sha256)
    for dependency in resolved.dependencies:
        asset = await session.get(ModelRow, dependency.model_id)
        unavailable = (
            asset is None
            or asset.kind not in AUXILIARY_IMAGE_KINDS
            or asset.backend != EngineBackend.AUXILIARY
            or asset.source != ModelSource.STUDIO
            or asset.state != ModelState.READY
            or asset.manifest_sha256 != dependency.sha256
        )
        if not unavailable and asset is not None:
            unavailable = not await _asset_authorized(
                session,
                principal,
                asset,
                explicit=resolved.spec.content_mode is ImageContentMode.EXPLICIT,
            )
        if unavailable and dependency.sha256 not in missing_dependencies:
            missing_dependencies.append(dependency.sha256)
    environment_matches = False
    if not missing_dependencies and resolved.pipeline_version == IMAGE_RUNTIME_VERSION:
        studios = (
            await session.scalars(
                select(NodeRow).where(
                    NodeRow.role == NodeRole.STUDIO,
                    NodeRow.reachability == Reachability.HEALTHY,
                )
            )
        ).all()
        environment_matches = any(
            image_environment_fingerprint(node, resolved.model_sha256)
            == resolved.environment_fingerprint
            for node in studios
        )
    if missing_dependencies:
        unavailable_reason = "model_components_unavailable"
    elif missing_inputs:
        unavailable_reason = "source_inputs_unavailable"
    elif resolved.pipeline_version != IMAGE_RUNTIME_VERSION:
        unavailable_reason = "runtime_version_changed"
    elif not environment_matches:
        unavailable_reason = "runtime_environment_changed"
    else:
        # Declared fields do not prove the local OS/MLX build or pixel equality.
        unavailable_reason = "runtime_environment_unverified"
    return ImageRecipeImport(
        recipe=recipe,
        settings=settings,
        exact_reproduction_available=False,
        missing_input_sha256=missing_inputs,
        missing_dependency_sha256=missing_dependencies,
        unavailable_reason=unavailable_reason,
    )
