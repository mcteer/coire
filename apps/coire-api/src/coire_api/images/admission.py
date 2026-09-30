"""Replay-safe queued image admission; not exposed until dispatch and cancel ship."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.auth import Principal, audit_actor
from coire_api.chat.files import new_job_id
from coire_api.db import ImageJobEventRow, ImageJobRow, ModelRow
from coire_api.images.authorization import authorize_live_image_action, preflight_image_action
from coire_api.images.job_capacity import reserve_image_job_capacity
from coire_api.images.presets import load_resolved_image_preset
from coire_api.images.quota import _QUOTA_LOCK
from coire_api.images.resolution import resolve_basic_image_spec
from coire_api.images.telemetry import ImageOperation, image_span
from coire_core.errors import ImageConflict, ImageForbidden, ImageValidationError
from coire_core.models.audit import AuditOutcome
from coire_core.models.images import (
    ImageCapabilityProfile,
    ImageContentMode,
    ImageJobEvent,
    ImageJobReceipt,
    ImageJobSettingsSnapshot,
    ImageJobState,
    ImageSubmitRequest,
    canonical_client_intent_hash,
)
from coire_core.models.registry import (
    AUXILIARY_IMAGE_KINDS,
    EngineBackend,
    ModelKind,
    ModelSource,
    ModelState,
    Visibility,
)
from coire_core.settings import Settings

_KEY = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_QUEUE_DEADLINE = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class ImageAdmissionPolicy:
    request: ImageSubmitRequest
    profile: ImageCapabilityProfile
    required_entitlements: frozenset[str]
    preset_id: uuid.UUID | None = None
    preset_revision: int | None = None


def _entitlements(value: object) -> frozenset[str]:
    if (
        not isinstance(value, list)
        or len(value) > 32
        or any(not isinstance(item, str) or not item or len(item) > 64 for item in value)
    ):
        raise ImageValidationError("invalid image entitlement policy")
    return frozenset(value)


def _ready_image_base(row: ModelRow | None, principal: Principal) -> ImageCapabilityProfile:
    if (
        row is None
        or row.kind != ModelKind.IMAGE_MODEL
        or row.backend != EngineBackend.MFLUX
        or row.source != ModelSource.STUDIO
        or row.state is not ModelState.READY
        or (row.visibility is not Visibility.PUBLISHED and not principal.is_admin)
        or row.image_capability_profile is None
        or row.manifest_sha256 is None
        or _DIGEST.fullmatch(row.manifest_sha256) is None
    ):
        raise ImageValidationError("image model unavailable")
    try:
        return ImageCapabilityProfile.model_validate(row.image_capability_profile)
    except ValidationError as exc:
        raise ImageValidationError("image model capability unavailable") from exc


async def _load_policy(
    session: AsyncSession, request: ImageSubmitRequest, principal: Principal
) -> ImageAdmissionPolicy:
    if request.preset_id is not None:
        preset = await load_resolved_image_preset(session, request, principal)
        assert preset.request.model_id is not None
        base = await session.get(
            ModelRow, preset.request.model_id, populate_existing=True, with_for_update=True
        )
        profile = _ready_image_base(base, principal)
        for dependency_id in sorted(preset.dependency_ids - {preset.request.model_id}):
            dependency = await session.get(
                ModelRow, dependency_id, populate_existing=True, with_for_update=True
            )
            _ready_image_dependency(dependency)
        return ImageAdmissionPolicy(
            request=preset.request,
            profile=profile,
            required_entitlements=preset.required_entitlements,
            preset_id=preset.preset_id,
            preset_revision=preset.preset_revision,
        )
    assert request.model_id is not None
    base = await session.get(
        ModelRow, request.model_id, populate_existing=True, with_for_update=True
    )
    profile = _ready_image_base(base, principal)
    assert base is not None
    required = set(_entitlements(base.entitlement))
    for dependency_id in sorted(profile.required_dependency_ids):
        dependency = await session.get(
            ModelRow, dependency_id, populate_existing=True, with_for_update=True
        )
        _ready_image_dependency(dependency)
        assert dependency is not None
        required.update(_entitlements(dependency.entitlement))
    effective_request = request
    if "explicit" in required:
        effective_request = request.model_copy(update={"content_mode": ImageContentMode.EXPLICIT})
    return ImageAdmissionPolicy(
        request=effective_request,
        profile=profile,
        required_entitlements=frozenset(required),
    )


def _ready_image_dependency(row: ModelRow | None) -> None:
    if (
        row is None
        or row.kind not in AUXILIARY_IMAGE_KINDS
        or row.backend != EngineBackend.AUXILIARY
        or row.source != ModelSource.STUDIO
        or row.state is not ModelState.READY
        or row.manifest_sha256 is None
        or _DIGEST.fullmatch(row.manifest_sha256) is None
    ):
        raise ImageValidationError("image dependency unavailable")


def _replay_entitlements(row: ImageJobRow) -> frozenset[str]:
    snapshot = row.authorization_snapshot
    if not isinstance(snapshot, dict):
        raise ImageConflict("image authorization snapshot unavailable")
    value = snapshot.get("required_entitlements")
    try:
        return _entitlements(value)
    except ImageValidationError as exc:
        raise ImageConflict("image authorization snapshot unavailable") from exc


async def admit_image_job(
    session: AsyncSession,
    principal: Principal,
    request: ImageSubmitRequest,
    idempotency_key: str,
    settings: Settings,
    *,
    random_seed: Callable[[], int] | None = None,
) -> ImageJobReceipt:
    """Commit job, capacity, first event and audit before returning a receipt."""
    if not settings.image_enabled:
        raise ImageForbidden()
    if _KEY.fullmatch(idempotency_key) is None:
        raise ImageValidationError("invalid image idempotency key")
    owner_id = preflight_image_action(principal, method="GET", origin=None, browser_origin="")
    intent_sha256 = canonical_client_intent_hash(request)
    with image_span(ImageOperation.SUBMIT):
        await session.execute(_QUOTA_LOCK)
        existing = await session.scalar(
            select(ImageJobRow)
            .where(
                ImageJobRow.owner_user_id == owner_id,
                ImageJobRow.idempotency_key == idempotency_key,
            )
            .with_for_update()
        )
        if existing is not None:
            if existing.intent_sha256 != intent_sha256:
                raise ImageConflict("image idempotency key reused with changed request")
            try:
                snapshot = ImageJobSettingsSnapshot.model_validate(existing.resolved_spec)
                state = ImageJobState(existing.state)
            except (ValidationError, ValueError, TypeError) as exc:
                raise ImageConflict("image job snapshot unavailable") from exc
            replay_required = _replay_entitlements(existing)
            await authorize_live_image_action(
                session,
                principal,
                explicit=(
                    snapshot.effective_spec.content_mode is ImageContentMode.EXPLICIT
                    or "explicit" in replay_required
                ),
                required_entitlements=replay_required,
            )
            return ImageJobReceipt(job_id=existing.id, state=state)

        policy = await _load_policy(session, request, principal)
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
        effective_request = (
            policy.request.model_copy(update={"content_mode": ImageContentMode.EXPLICIT})
            if explicit
            else policy.request
        )
        spec = resolve_basic_image_spec(effective_request, policy.profile, random_seed=random_seed)
        held_bytes = await reserve_image_job_capacity(session, owner_id, spec.n, settings)
        now = datetime.now(UTC)
        job_id = new_job_id()
        snapshot = ImageJobSettingsSnapshot(effective_spec=spec)
        row = ImageJobRow(
            id=job_id,
            owner_user_id=owner_id,
            originating_key_id=principal.api_key_id,
            originating_key_version=principal.credential_version,
            browser_identity=None,
            idempotency_key=idempotency_key,
            intent_sha256=intent_sha256,
            submitted_spec=request.model_dump(mode="json"),
            resolved_spec=snapshot.model_dump(mode="json"),
            state=ImageJobState.QUEUED,
            version=1,
            attempt=1,
            fence=0,
            workflow_id=f"image-{job_id}",
            preset_id=policy.preset_id,
            preset_revision=policy.preset_revision,
            reservation_ids=[],
            queued_at=now,
            deadline_at=now + _QUEUE_DEADLINE,
            progress=0.0,
            receipt_state="pending",
            cleanup_state="pending",
            authorization_snapshot={
                "required_entitlements": sorted(policy.required_entitlements),
                "explicit": explicit,
                "output_hold_bytes": held_bytes,
            },
            created_at=now,
            updated_at=now,
        )
        event = ImageJobEvent(
            job_id=job_id,
            sequence=1,
            at=now,
            type="queued",
            state=ImageJobState.QUEUED,
        )
        session.add(row)
        session.add(
            ImageJobEventRow(
                job_id=job_id,
                sequence=1,
                event_type="queued",
                payload=event.model_dump(mode="json"),
                created_at=now,
            )
        )
        actor, actor_type, actor_user_id = audit_actor(principal)
        await write_audit(
            session,
            actor=actor,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
            action="image.submit",
            target_type="image_job",
            target_id=job_id,
            outcome=AuditOutcome.OK,
            context={
                "model_id": str(spec.model_id),
                "explicit": explicit,
                "required_entitlements": sorted(policy.required_entitlements),
                "output_count": spec.n,
            },
        )
        await session.commit()
        return ImageJobReceipt(job_id=job_id, state=ImageJobState.QUEUED, event_cursor="1")
