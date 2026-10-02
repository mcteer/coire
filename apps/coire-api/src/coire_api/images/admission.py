"""Replay-safe queued image admission. Disabled unless image admission is enabled."""

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
from coire_api.db import ImageInputRow, ImageJobEventRow, ImageJobRow, ModelRow
from coire_api.images.authorization import authorize_live_image_action, preflight_image_action
from coire_api.images.job_capacity import reserve_image_job_capacity
from coire_api.images.presets import load_resolved_image_preset
from coire_api.images.quota import _QUOTA_LOCK
from coire_api.images.resolution import resolve_basic_image_spec
from coire_api.images.telemetry import ImageOperation, image_span
from coire_core.errors import ImageConflict, ImageForbidden, ImageValidationError
from coire_core.models.audit import AuditOutcome
from coire_core.models.images import (
    GENERATION_INPUT_MAX_BYTES,
    ImageCapabilityProfile,
    ImageContentMode,
    ImageJobEvent,
    ImageJobReceipt,
    ImageJobSettingsSnapshot,
    ImageJobState,
    ImageSpec,
    ImageSubmitRequest,
    canonical_client_intent_hash,
    image_input_bindings,
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


def _ready_image_base(row: ModelRow | None) -> ImageCapabilityProfile:
    if (
        row is None
        or row.kind != ModelKind.IMAGE_MODEL
        or row.backend != EngineBackend.MFLUX
        or row.source != ModelSource.STUDIO
        or row.state is not ModelState.READY
        or row.visibility is not Visibility.PUBLISHED
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
        profile = _ready_image_base(base)
        for dependency_id in sorted(preset.dependency_ids - {preset.request.model_id}):
            dependency = await session.get(
                ModelRow, dependency_id, populate_existing=True, with_for_update=True
            )
            _ready_image_dependency(dependency, preset.request.model_id)
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
    profile = _ready_image_base(base)
    assert base is not None
    required = set(_entitlements(base.entitlement))
    selected_loras = {item.model_id for item in request.loras or []}
    for dependency_id in sorted(set(profile.required_dependency_ids) | selected_loras):
        dependency = await session.get(
            ModelRow, dependency_id, populate_existing=True, with_for_update=True
        )
        _ready_image_dependency(dependency, request.model_id)
        assert dependency is not None
        if dependency_id in selected_loras and (
            dependency.kind is not ModelKind.IMAGE_LORA
            or dependency.visibility is not Visibility.PUBLISHED
        ):
            raise ImageValidationError("image LoRA unavailable")
        required.update(_entitlements(dependency.entitlement))
    effective_request = request
    if "explicit" in required:
        effective_request = request.model_copy(update={"content_mode": ImageContentMode.EXPLICIT})
    return ImageAdmissionPolicy(
        request=effective_request,
        profile=profile,
        required_entitlements=frozenset(required),
    )


def _ready_image_dependency(row: ModelRow | None, base_model_id: uuid.UUID) -> None:
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
    if row.kind in {ModelKind.IMAGE_LORA, ModelKind.CONTROL_MODEL} and (
        not isinstance(row.capability_profile, dict)
        or row.capability_profile.get("compatible_base_model_id") != str(base_model_id)
    ):
        raise ImageValidationError("image dependency is incompatible with the selected base")


def _replay_entitlements(row: ImageJobRow) -> frozenset[str]:
    snapshot = row.authorization_snapshot
    if not isinstance(snapshot, dict):
        raise ImageConflict("image authorization snapshot unavailable")
    value = snapshot.get("required_entitlements")
    try:
        return _entitlements(value)
    except ImageValidationError as exc:
        raise ImageConflict("image authorization snapshot unavailable") from exc


async def _retain_generation_inputs(
    session: AsyncSession, owner_id: uuid.UUID, spec: ImageSpec
) -> None:
    """Retain every owner input together before admitting a generation job."""
    try:
        bindings = image_input_bindings(spec)
    except ValueError as exc:
        raise ImageValidationError(str(exc)) from exc
    rows: list[ImageInputRow] = []
    for input_id, purpose in bindings:
        row = await session.get(
            ImageInputRow, input_id, populate_existing=True, with_for_update=True
        )
        if (
            row is None
            or row.owner_user_id != owner_id
            or row.purpose != purpose
            or row.state != "ready"
            or row.deleted_at is not None
            or row.normalized_key != str(input_id)
            or row.normalized_sha256 is None
            or _DIGEST.fullmatch(row.normalized_sha256) is None
            or row.normalized_bytes is None
            or not 0 < row.normalized_bytes <= GENERATION_INPUT_MAX_BYTES
            or row.normalized_width != spec.width
            or row.normalized_height != spec.height
        ):
            raise ImageValidationError(
                f"{purpose} image is unavailable or has different dimensions"
            )
        rows.append(row)
    for row in rows:
        row.active_references += 1


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
        if policy.profile.required_dependency_ids:
            raise ImageValidationError("image model requires an unsupported local component")
        effective_request = (
            policy.request.model_copy(update={"content_mode": ImageContentMode.EXPLICIT})
            if explicit
            else policy.request
        )
        spec = resolve_basic_image_spec(effective_request, policy.profile, random_seed=random_seed)
        await _retain_generation_inputs(session, owner_id, spec)
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
