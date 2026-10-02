"""Current, entitlement-filtered image base picker without acquisition side effects."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import ModelRow
from coire_api.images.admission import _load_policy
from coire_api.images.authorization import authorize_live_image_action
from coire_core.errors import ImageConflict, ImageForbidden, ImageNotFound, ImageValidationError
from coire_core.models.images import (
    ImageAdapterOption,
    ImageCapabilityProfile,
    ImageContentMode,
    ImageControl,
    ImageLora,
    ImageMode,
    ImageModelList,
    ImageModelOption,
    ImageSubmitRequest,
    ImageUpscale,
)
from coire_core.models.registry import EngineBackend, ModelKind, ModelSource, ModelState, Visibility


async def list_eligible_image_models(session: AsyncSession, principal: Principal) -> ImageModelList:
    """Expose only ready published bases whose hidden dependencies are usable now."""
    await authorize_live_image_action(session, principal)
    rows = (
        await session.scalars(
            select(ModelRow)
            .where(
                ModelRow.kind == ModelKind.IMAGE_MODEL,
                ModelRow.backend == EngineBackend.MFLUX,
                ModelRow.source == ModelSource.STUDIO,
                ModelRow.state == ModelState.READY,
                ModelRow.visibility == Visibility.PUBLISHED,
            )
            .order_by(ModelRow.display_name, ModelRow.id)
            .limit(100)
        )
    ).all()
    adapters = (
        await session.scalars(
            select(ModelRow)
            .where(
                ModelRow.kind == ModelKind.IMAGE_LORA,
                ModelRow.backend == EngineBackend.AUXILIARY,
                ModelRow.source == ModelSource.STUDIO,
                ModelRow.state == ModelState.READY,
                ModelRow.visibility == Visibility.PUBLISHED,
            )
            .order_by(ModelRow.display_name, ModelRow.id)
            .limit(400)
        )
    ).all()
    upscale_assets = (
        await session.scalars(
            select(ModelRow)
            .where(
                ModelRow.kind == ModelKind.UPSCALE_MODEL,
                ModelRow.backend == EngineBackend.AUXILIARY,
                ModelRow.source == ModelSource.STUDIO,
                ModelRow.state == ModelState.READY,
                ModelRow.visibility == Visibility.PUBLISHED,
            )
            .order_by(ModelRow.display_name, ModelRow.id)
            .limit(100)
        )
    ).all()
    control_assets = (
        await session.scalars(
            select(ModelRow)
            .where(
                ModelRow.kind == ModelKind.CONTROL_MODEL,
                ModelRow.backend == EngineBackend.AUXILIARY,
                ModelRow.source == ModelSource.STUDIO,
                ModelRow.state == ModelState.READY,
                ModelRow.visibility == Visibility.PUBLISHED,
            )
            .order_by(ModelRow.display_name, ModelRow.id)
            .limit(100)
        )
    ).all()
    items: list[ImageModelOption] = []
    for row in rows:
        try:
            policy = await _load_policy(
                session,
                ImageSubmitRequest(model_id=row.id, prompt="image model eligibility check"),
                principal,
            )
            explicit = (
                policy.request.content_mode is ImageContentMode.EXPLICIT
                or "explicit" in policy.required_entitlements
            )
            await authorize_live_image_action(
                session,
                principal,
                explicit=explicit,
                required_entitlements=policy.required_entitlements,
            )
        except (ImageForbidden, ImageConflict, ImageNotFound, ImageValidationError):
            continue
        if (
            ImageMode.TXT2IMG not in policy.profile.modes
            or policy.profile.min_guidance > 0
            or policy.profile.default_guidance != 0
            or policy.profile.required_dependency_ids
        ):
            continue
        eligible_controls: list[ImageAdapterOption] = []
        for asset in control_assets:
            if (
                asset.kind is not ModelKind.CONTROL_MODEL
                or asset.visibility is not Visibility.PUBLISHED
                or not isinstance(asset.capability_profile, dict)
                or asset.capability_profile.get("compatible_base_model_id") != str(row.id)
            ):
                continue
            try:
                selected = await _load_policy(
                    session,
                    ImageSubmitRequest(
                        model_id=row.id,
                        mode=ImageMode.CONTROL,
                        prompt="image control eligibility check",
                        control=ImageControl(image_id=uuid.uuid4(), model_id=asset.id),
                    ),
                    principal,
                )
                explicit = (
                    selected.request.content_mode is ImageContentMode.EXPLICIT
                    or "explicit" in selected.required_entitlements
                )
                await authorize_live_image_action(
                    session,
                    principal,
                    explicit=explicit,
                    required_entitlements=selected.required_entitlements,
                )
            except (ImageForbidden, ImageConflict, ImageNotFound, ImageValidationError):
                continue
            eligible_controls.append(
                ImageAdapterOption(id=asset.id, display_name=asset.display_name)
            )
        # Advertise only settings accepted by the current fixed native worker.
        basic = ImageCapabilityProfile.model_validate(
            {
                **policy.profile.model_dump(),
                "modes": (
                    tuple(
                        mode
                        for mode in (ImageMode.TXT2IMG, ImageMode.IMG2IMG)
                        if mode in policy.profile.modes
                    )
                    + ((ImageMode.CONTROL,) if eligible_controls else ())
                ),
                "min_guidance": 0,
                "max_guidance": 0,
                "max_loras": policy.profile.max_loras,
                "supports_negative_prompt": False,
                "required_dependency_ids": (),
            }
        )
        eligible_loras: list[ImageAdapterOption] = []
        if basic.max_loras:
            for adapter in adapters:
                if (
                    adapter.kind is not ModelKind.IMAGE_LORA
                    or adapter.visibility is not Visibility.PUBLISHED
                    or not isinstance(adapter.capability_profile, dict)
                    or adapter.capability_profile.get("compatible_base_model_id") != str(row.id)
                ):
                    continue
                try:
                    selected = await _load_policy(
                        session,
                        ImageSubmitRequest(
                            model_id=row.id,
                            prompt="image adapter eligibility check",
                            loras=[ImageLora(model_id=adapter.id, scale=Decimal(1))],
                        ),
                        principal,
                    )
                    explicit = (
                        selected.request.content_mode is ImageContentMode.EXPLICIT
                        or "explicit" in selected.required_entitlements
                    )
                    await authorize_live_image_action(
                        session,
                        principal,
                        explicit=explicit,
                        required_entitlements=selected.required_entitlements,
                    )
                except (ImageForbidden, ImageConflict, ImageNotFound, ImageValidationError):
                    continue
                eligible_loras.append(
                    ImageAdapterOption(id=adapter.id, display_name=adapter.display_name)
                )
        eligible_upscalers: list[ImageAdapterOption] = []
        for asset in upscale_assets:
            if (
                asset.kind is not ModelKind.UPSCALE_MODEL
                or asset.visibility is not Visibility.PUBLISHED
            ):
                continue
            try:
                selected = await _load_policy(
                    session,
                    ImageSubmitRequest(
                        model_id=row.id,
                        prompt="image upscale eligibility check",
                        upscale=ImageUpscale(model_id=asset.id, factor=2),
                    ),
                    principal,
                )
                explicit = (
                    selected.request.content_mode is ImageContentMode.EXPLICIT
                    or "explicit" in selected.required_entitlements
                )
                await authorize_live_image_action(
                    session,
                    principal,
                    explicit=explicit,
                    required_entitlements=selected.required_entitlements,
                )
            except (ImageForbidden, ImageConflict, ImageNotFound, ImageValidationError):
                continue
            eligible_upscalers.append(
                ImageAdapterOption(id=asset.id, display_name=asset.display_name)
            )
        items.append(
            ImageModelOption(
                id=row.id,
                slug=row.slug,
                display_name=row.display_name,
                capability=basic,
                required_dependency_count=len(policy.profile.required_dependency_ids),
                loras=tuple(eligible_loras),
                upscalers=tuple(eligible_upscalers),
                controls=tuple(eligible_controls),
            )
        )
    return ImageModelList(items=items)
