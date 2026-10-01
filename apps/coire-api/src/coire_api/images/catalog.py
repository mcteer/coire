"""Current, entitlement-filtered image base picker without acquisition side effects."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import ModelRow
from coire_api.images.admission import _load_policy
from coire_api.images.authorization import authorize_live_image_action
from coire_core.errors import ImageConflict, ImageForbidden, ImageNotFound, ImageValidationError
from coire_core.models.images import (
    ImageCapabilityProfile,
    ImageContentMode,
    ImageMode,
    ImageModelList,
    ImageModelOption,
    ImageSubmitRequest,
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
        ):
            continue
        # Advertise only settings accepted by the current fixed native worker.
        basic = ImageCapabilityProfile.model_validate(
            {
                **policy.profile.model_dump(),
                "modes": tuple(
                    mode
                    for mode in (ImageMode.TXT2IMG, ImageMode.IMG2IMG)
                    if mode in policy.profile.modes
                ),
                "min_guidance": 0,
                "max_guidance": 0,
                "max_loras": 0,
                "supports_negative_prompt": False,
                "required_dependency_ids": (),
            }
        )
        items.append(
            ImageModelOption(
                id=row.id,
                slug=row.slug,
                display_name=row.display_name,
                capability=basic,
                required_dependency_count=len(policy.profile.required_dependency_ids),
            )
        )
    return ImageModelList(items=items)
