"""Place one queued image job on a Studio without starting a second attempt."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.db import (
    ApiKeyRow,
    EntitlementRow,
    ImageExecutionLeaseRow,
    ImageInputRow,
    ImageJobEventRow,
    ImageJobRow,
    InstanceMemberRow,
    MemoryReservationRow,
    ModelInstanceRow,
    ModelRow,
    NodeRow,
    UserRow,
)
from coire_api.images.input_references import release_image_input_references
from coire_api.images.job_capacity import mark_image_job_started, release_pending_image_job_capacity
from coire_api.images.jobs import _policy
from coire_api.images.quota import _QUOTA_LOCK
from coire_api.placement.service import lock_nodes_for_admission
from coire_core.errors import ImageConflict
from coire_core.models.audit import AuditOutcome
from coire_core.models.image_worker import (
    ImageWorkerLoadRequest,
    NodeImageInputManifest,
    NodeImageStartRequest,
)
from coire_core.models.images import (
    ImageCapabilityProfile,
    ImageInputDigest,
    ImageJobEvent,
    ImageJobState,
    ImageManifestDigest,
    ImageMode,
    ImageSpec,
    ResolvedImageSpec,
    canonical_spec_hash,
    expand_image_seeds,
    image_input_bindings,
)
from coire_core.models.instance import InstanceState
from coire_core.models.node import NodeRole, Reachability
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.models.registry import EngineBackend, ModelKind, ModelSource, ModelState, Visibility
from coire_core.settings import Settings
from coire_scheduler.image_admission import (
    chat_mix_allowed,
    image_available_bytes,
    image_environment_fingerprint,
    node_thermal_alarm,
)

IMAGE_PREFERRED_NODE = "coire-edge-b"
IMAGE_RUNTIME_VERSION = "mflux-0.20.0"
_ACTIVE_CHAT = (
    InstanceState.RESERVING,
    InstanceState.LAUNCHING,
    InstanceState.WARMING,
    InstanceState.READY,
    InstanceState.DRAINING,
)
_REUSABLE_WORKER = (InstanceState.LAUNCHING, InstanceState.WARMING, InstanceState.READY)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")


@dataclass(frozen=True, slots=True)
class ImageNodeCandidate:
    name: str
    node_id: uuid.UUID
    healthy: bool
    memory_total_bytes: int
    image_busy: bool
    chat_unmeasured: bool
    thermal_alarm: bool = False


@dataclass(frozen=True, slots=True)
class PreparedImageDispatch:
    node: str
    load: ImageWorkerLoadRequest
    start: NodeImageStartRequest


def choose_image_node(
    policy: str, estimate_bytes: int, nodes: list[ImageNodeCandidate]
) -> ImageNodeCandidate | None:
    """Prefer Studio B. A pinned node is never silently redirected."""
    if estimate_bytes < 1:
        return None
    kind, _, target = policy.partition(":")
    if kind == "sharded":
        return None
    if target and target != "auto":
        match = next((node for node in nodes if node.name == target), None)
        if (
            match is None
            or not match.healthy
            or match.image_busy
            or match.chat_unmeasured
            or match.thermal_alarm
            or match.memory_total_bytes < estimate_bytes
        ):
            return None
        return match
    fitting = [
        node
        for node in nodes
        if node.healthy
        and not node.image_busy
        and not node.chat_unmeasured
        and not node.thermal_alarm
        and node.memory_total_bytes >= estimate_bytes
    ]
    if not fitting:
        return None
    return min(fitting, key=lambda node: (node.name != IMAGE_PREFERRED_NODE, node.name))


def _placed(row: ImageJobRow) -> bool:
    return row.fence != 0 or row.selected_node_id is not None or row.instance_id is not None


def image_worker_hold_bytes(model_estimate_bytes: int | None, settings: Settings) -> int:
    """Reserve CPU classification headroom alongside the resident image estimate."""
    return max(model_estimate_bytes or 0, 1) + settings.image_classifier_memory_bytes


async def _bound_loras(
    session: AsyncSession,
    spec: ImageSpec,
    base: ModelRow,
    *,
    from_preset: bool,
) -> tuple[tuple[ImageManifestDigest, ...], int] | None:
    """Bind ordered, compatible copies and their incremental measured hold."""
    manifests: list[ImageManifestDigest] = []
    overhead = 0
    for selected in spec.loras:
        dependency = await session.get(ModelRow, selected.model_id, populate_existing=True)
        if (
            dependency is None
            or dependency.kind is not ModelKind.IMAGE_LORA
            or (not from_preset and dependency.visibility is not Visibility.PUBLISHED)
            or dependency.backend is not EngineBackend.AUXILIARY
            or dependency.source is not ModelSource.STUDIO
            or dependency.state is not ModelState.READY
            or dependency.manifest_sha256 is None
            or _DIGEST.fullmatch(dependency.manifest_sha256) is None
            or dependency.source_revision is None
            or _REVISION.fullmatch(dependency.source_revision) is None
            or not isinstance(dependency.capability_profile, dict)
            or dependency.capability_profile.get("compatible_base_model_id") != str(base.id)
            or dependency.memory_estimate_bytes is None
            or dependency.memory_estimate_bytes < (base.memory_estimate_bytes or 0)
        ):
            return None
        overhead += dependency.memory_estimate_bytes - (base.memory_estimate_bytes or 0)
        manifests.append(
            ImageManifestDigest(
                model_id=dependency.id,
                slug=dependency.slug,
                revision=dependency.source_revision,
                sha256=dependency.manifest_sha256,
            )
        )
    return tuple(manifests), overhead


async def fail_image_attempt(session: AsyncSession, job_id: str, code: str) -> bool:
    """Record one terminal failure. A missing journal is not a reason to generate again."""
    now = datetime.now(UTC)
    await session.execute(_QUOTA_LOCK)
    row = await session.get(ImageJobRow, job_id, populate_existing=True, with_for_update=True)
    if row is None or row.state in {"succeeded", "failed", "cancelled", "cancelling"}:
        return False
    if row.state not in {"queued", "reserving", "running"}:
        return False
    snapshot, _, _ = _policy(row)
    if _placed(row) or snapshot.resolved is not None:
        raise ImageConflict("placed image attempt needs node termination proof")
    held = row.authorization_snapshot.get("output_hold_bytes")
    if not isinstance(held, int) or isinstance(held, bool) or held <= 0:
        raise ImageConflict("image capacity hold unavailable")
    await release_pending_image_job_capacity(
        session, row.owner_user_id, snapshot.effective_spec.n, held
    )
    await release_image_input_references(session, row)
    leases = (
        await session.scalars(
            select(ImageExecutionLeaseRow)
            .where(
                ImageExecutionLeaseRow.job_id == job_id,
                ImageExecutionLeaseRow.released_at.is_(None),
            )
            .with_for_update()
        )
    ).all()
    for lease in leases:
        lease.released_at = now
        lease.release_evidence = {"reason": code}
    latest = await session.scalar(
        select(func.max(ImageJobEventRow.sequence)).where(ImageJobEventRow.job_id == job_id)
    )
    if latest is None or latest < 1:
        raise ImageConflict("image event history unavailable")
    row.state = ImageJobState.FAILED
    row.safe_failure_code = code
    row.cleanup_state = "cleaned"
    row.receipt_state = "none"
    row.updated_at = now
    row.finished_at = now
    row.version += 1
    event = ImageJobEvent(
        job_id=job_id,
        sequence=latest + 1,
        at=now,
        type="error",
        state=ImageJobState.FAILED,
        safe_code=code,
    )
    session.add(
        ImageJobEventRow(
            job_id=job_id,
            sequence=event.sequence,
            event_type=event.type,
            payload=event.model_dump(mode="json"),
            created_at=now,
        )
    )
    await write_audit(
        session,
        actor="coire-scheduler",
        action="image.attempt.failed",
        target_type="image_job",
        target_id=job_id,
        outcome=AuditOutcome.OK,
        context={"reason": code},
    )
    return True


async def _access_current(session: AsyncSession, row: ImageJobRow, *, explicit: bool) -> bool:
    user = await session.get(UserRow, row.owner_user_id, populate_existing=True)
    if user is None or not user.active:
        return False
    if row.originating_key_id is not None:
        key = await session.get(ApiKeyRow, row.originating_key_id, populate_existing=True)
        if (
            key is None
            or key.user_id != row.owner_user_id
            or key.revoked_at is not None
            or key.credential_version != row.originating_key_version
            or "images" not in (key.scopes or [])
            or (explicit and "images:explicit" not in (key.scopes or []))
        ):
            return False
    snapshot, required, _ = _policy(row)
    del snapshot
    names = required | ({"explicit"} if explicit else set())
    if not names:
        return True
    active = frozenset(
        (
            await session.scalars(
                select(EntitlementRow.name).where(
                    EntitlementRow.user_id == row.owner_user_id,
                    EntitlementRow.revoked_at.is_(None),
                )
            )
        ).all()
    )
    return names <= active


async def _cancel_revoked_locked(session: AsyncSession, row: ImageJobRow, now: datetime) -> None:
    """Keep every hold until the fenced node proves termination and cleanup."""
    row.state = ImageJobState.CANCELLING
    row.cancel_requested_at = now
    row.updated_at = now
    row.version += 1
    await write_audit(
        session,
        actor="coire-scheduler",
        action="image.authorization_revoked",
        target_type="image_job",
        target_id=row.id,
        outcome=AuditOutcome.OK,
        context={"reason": "authorization_revoked"},
    )


async def request_revoked_image_cancel(session: AsyncSession, job_id: str) -> bool:
    """Arbitrate live revocation against publication under the job and quota locks."""
    await session.execute(_QUOTA_LOCK)
    row = await session.get(ImageJobRow, job_id, populate_existing=True, with_for_update=True)
    if (
        row is None
        or row.state not in {ImageJobState.RESERVING, ImageJobState.RUNNING}
        or row.cancel_requested_at is not None
    ):
        return False
    _, _, explicit = _policy(row)
    if await _access_current(session, row, explicit=explicit):
        return False
    await _cancel_revoked_locked(session, row, datetime.now(UTC))
    return True


async def _cancel_thermal_locked(session: AsyncSession, row: ImageJobRow, now: datetime) -> None:
    """Fence publication and retain every hold until node termination is proved."""
    row.state = ImageJobState.CANCELLING
    row.cancel_requested_at = now
    row.updated_at = now
    row.version += 1
    await write_audit(
        session,
        actor="coire-scheduler",
        action="image.thermal_cancel",
        target_type="image_job",
        target_id=row.id,
        outcome=AuditOutcome.OK,
        context={"reason": "node_thermal_alarm", "node_id": str(row.selected_node_id)},
    )


async def request_thermal_image_cancel(session: AsyncSession, job_id: str) -> bool:
    """Request a fenced stop only for a recently overheated selected Studio."""
    await session.execute(_QUOTA_LOCK)
    row = await session.get(ImageJobRow, job_id, populate_existing=True, with_for_update=True)
    if (
        row is None
        or row.state not in {ImageJobState.RESERVING, ImageJobState.RUNNING}
        or row.selected_node_id is None
        or row.cancel_requested_at is not None
    ):
        return False
    now = datetime.now(UTC)
    if not await node_thermal_alarm(session, row.selected_node_id, now):
        return False
    await _cancel_thermal_locked(session, row, now)
    return True


def _ready_base(row: ModelRow | None) -> ImageCapabilityProfile | None:
    if (
        row is None
        or row.kind != ModelKind.IMAGE_MODEL
        or row.backend != "mflux"
        or row.source != ModelSource.STUDIO
        or row.state is not ModelState.READY
        or row.visibility is not Visibility.PUBLISHED
        or row.manifest_sha256 is None
        or _DIGEST.fullmatch(row.manifest_sha256) is None
        or row.image_capability_profile is None
    ):
        return None
    try:
        return ImageCapabilityProfile.model_validate(row.image_capability_profile)
    except ValidationError:
        return None


async def _chat_unmeasured(
    session: AsyncSession, node_id: uuid.UUID, image_model_id: uuid.UUID, now: datetime
) -> bool:
    resident = {
        str(variant_id)
        for variant_id in (
            await session.scalars(
                select(ModelInstanceRow.variant_id)
                .join(InstanceMemberRow, InstanceMemberRow.instance_id == ModelInstanceRow.id)
                .join(ModelRow, ModelRow.id == ModelInstanceRow.model_id)
                .where(
                    InstanceMemberRow.node_id == node_id,
                    ModelRow.kind == ModelKind.LANGUAGE_MODEL,
                    ModelInstanceRow.state.in_(_ACTIVE_CHAT),
                    ModelInstanceRow.variant_id.is_not(None),
                )
            )
        ).all()
    }
    return not await chat_mix_allowed(session, node_id, image_model_id, resident, now)


async def _image_busy(session: AsyncSession, node_id: uuid.UUID, model_id: uuid.UUID) -> bool:
    leased = await session.scalar(
        select(ImageExecutionLeaseRow.id)
        .where(
            ImageExecutionLeaseRow.node_id == node_id,
            ImageExecutionLeaseRow.mode == "image",
            ImageExecutionLeaseRow.released_at.is_(None),
        )
        .limit(1)
    )
    if leased is not None:
        return True
    draining = await session.scalar(
        select(ModelInstanceRow.id)
        .join(InstanceMemberRow, InstanceMemberRow.instance_id == ModelInstanceRow.id)
        .where(
            InstanceMemberRow.node_id == node_id,
            ModelInstanceRow.policy.like("image:%"),
            ModelInstanceRow.state == InstanceState.DRAINING,
        )
        .limit(1)
    )
    if draining is not None:
        return True
    other = await session.scalar(
        select(ModelInstanceRow.id)
        .join(InstanceMemberRow, InstanceMemberRow.instance_id == ModelInstanceRow.id)
        .where(
            InstanceMemberRow.node_id == node_id,
            ModelInstanceRow.policy.like("image:%"),
            ModelInstanceRow.model_id != model_id,
            ModelInstanceRow.state.in_(_REUSABLE_WORKER),
        )
        .limit(1)
    )
    return other is not None


async def _bound_inputs(
    session: AsyncSession, owner_id: uuid.UUID, spec: ImageSpec
) -> tuple[NodeImageInputManifest, ...]:
    """Translate retained owner inputs to path-free, exact node manifests."""
    try:
        bindings = image_input_bindings(spec)
    except ValueError as exc:
        raise ImageConflict("bound image input is unavailable") from exc
    manifests: list[NodeImageInputManifest] = []
    for input_id, purpose in bindings:
        row = await session.get(ImageInputRow, input_id, populate_existing=True)
        if (
            row is None
            or row.owner_user_id != owner_id
            or row.purpose != purpose
            or row.state != "ready"
            or row.deleted_at is not None
            or row.active_references < 1
            or row.normalized_key != str(input_id)
            or row.normalized_sha256 is None
            or _DIGEST.fullmatch(row.normalized_sha256) is None
            or row.normalized_bytes is None
            or not 0 < row.normalized_bytes <= 10 * 1024 * 1024
            or row.normalized_width != spec.width
            or row.normalized_height != spec.height
        ):
            raise ImageConflict("bound image input is unavailable")
        manifests.append(
            NodeImageInputManifest(
                input_id=row.id,
                purpose=purpose,
                sha256=row.normalized_sha256,
                byte_count=row.normalized_bytes,
                width=spec.width,
                height=spec.height,
            )
        )
    return tuple(manifests)


def _commands(
    row: ImageJobRow,
    node_name: str,
    instance_id: uuid.UUID,
    resolved: ResolvedImageSpec,
    reservation_bytes: int,
    settings: Settings,
    slug: str,
    inputs: tuple[NodeImageInputManifest, ...] = (),
) -> PreparedImageDispatch:
    load = ImageWorkerLoadRequest(
        slug=slug,
        model_id=resolved.spec.model_id,
        variant_id=resolved.spec.variant_id,
        instance_id=instance_id,
        manifest_sha256=resolved.model_sha256,
        reservation_bytes=reservation_bytes,
        runtime_version=IMAGE_RUNTIME_VERSION,
        idle_ttl_seconds=settings.image_worker_idle_ttl_s,
    )
    start = NodeImageStartRequest(
        job_id=row.id,
        attempt=row.attempt,
        fence=row.fence,
        node=node_name,
        model_id=resolved.spec.model_id,
        instance_id=instance_id,
        resolved=resolved,
        inputs=inputs,
        deadline_at=row.deadline_at,
        reservation_bytes=reservation_bytes,
    )
    return PreparedImageDispatch(node=node_name, load=load, start=start)


async def _resume(
    session: AsyncSession, row: ImageJobRow, settings: Settings
) -> PreparedImageDispatch:
    if row.selected_node_id is None or row.instance_id is None or row.fence < 1:
        raise ImageConflict("image placement is incomplete")
    node = await session.get(NodeRow, row.selected_node_id)
    snapshot, _, _ = _policy(row)
    if node is None or snapshot.resolved is None:
        raise ImageConflict("image runtime is not bound")
    model = await session.get(ModelRow, snapshot.resolved.spec.model_id)
    if model is None or model.manifest_sha256 != snapshot.resolved.model_sha256:
        raise ImageConflict("image model manifest changed after placement")
    reservation = row.authorization_snapshot.get("reservation_bytes")
    if not isinstance(reservation, int) or isinstance(reservation, bool) or reservation < 1:
        raise ImageConflict("image reservation is unavailable")
    return _commands(
        row,
        node.name,
        row.instance_id,
        snapshot.resolved,
        reservation,
        settings,
        model.slug,
        await _bound_inputs(session, row.owner_user_id, snapshot.resolved.spec),
    )


async def prepare_image_dispatch(
    session: AsyncSession,
    job_id: str,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> PreparedImageDispatch | None:
    """Commit placement once, then return the same node command on every retry."""
    current = now or datetime.now(UTC)
    await session.execute(_QUOTA_LOCK)
    row = await session.get(ImageJobRow, job_id, populate_existing=True, with_for_update=True)
    if row is None or row.cancel_requested_at is not None:
        return None
    if row.state in {"reserving", "running"} and _placed(row):
        _, _, explicit = _policy(row)
        if not await _access_current(session, row, explicit=explicit):
            await _cancel_revoked_locked(session, row, current)
            return None
        if row.selected_node_id is not None and await node_thermal_alarm(
            session, row.selected_node_id, current
        ):
            await _cancel_thermal_locked(session, row, current)
            return None
        return await _resume(session, row, settings)
    if row.state != ImageJobState.QUEUED or _placed(row) or row.deadline_at <= current:
        return None
    snapshot, _, explicit = _policy(row)
    if snapshot.resolved is not None:
        raise ImageConflict("image queue dispatch has bound runtime")
    spec = snapshot.effective_spec
    if (
        spec.mode not in {ImageMode.TXT2IMG, ImageMode.IMG2IMG}
        or spec.guidance != 0
        or spec.negative_prompt is not None
        or spec.seed is None
        or spec.variant_id is not None
        or spec.mask_id is not None
        or spec.control is not None
        or spec.upscale is not None
    ):
        await fail_image_attempt(session, job_id, "unsupported_mode")
        return None
    if not await _access_current(session, row, explicit=explicit):
        await fail_image_attempt(session, job_id, "authorization_revoked")
        return None
    model = await session.get(ModelRow, spec.model_id, populate_existing=True)
    profile = _ready_base(model)
    if model is None or profile is None or model.manifest_sha256 is None:
        await fail_image_attempt(session, job_id, "model_unavailable")
        return None
    try:
        input_manifests = await _bound_inputs(session, row.owner_user_id, spec)
    except ImageConflict:
        await fail_image_attempt(session, job_id, "input_unavailable")
        return None
    bound_loras = await _bound_loras(session, spec, model, from_preset=row.preset_id is not None)
    if bound_loras is None:
        await fail_image_attempt(session, job_id, "model_unavailable")
        return None
    dependencies, adapter_overhead = bound_loras
    studios = (await session.scalars(select(NodeRow).where(NodeRow.role == NodeRole.STUDIO))).all()
    # Chat placement uses the same transaction-scoped node locks. Acquire all
    # candidates before reading image/chat occupancy so neither path can race
    # a new incompatible resident load into the chosen Studio.
    await lock_nodes_for_admission(session, [studio.id for studio in studios])
    candidates: list[ImageNodeCandidate] = []
    reservation = image_worker_hold_bytes(
        (model.memory_estimate_bytes or 0) + adapter_overhead, settings
    )
    for studio in studios:
        candidates.append(
            ImageNodeCandidate(
                name=studio.name,
                node_id=studio.id,
                healthy=studio.reachability is Reachability.HEALTHY,
                memory_total_bytes=await image_available_bytes(
                    session,
                    studio,
                    model.id,
                    reservation,
                    budget_fraction=settings.node_memory_budget_fraction,
                ),
                image_busy=await _image_busy(session, studio.id, model.id),
                chat_unmeasured=await _chat_unmeasured(session, studio.id, model.id, current),
                thermal_alarm=await node_thermal_alarm(session, studio.id, current),
            )
        )
    chosen = choose_image_node(model.placement_policy, reservation, candidates)
    if chosen is None or re.fullmatch(r"coire-[a-z0-9-]{1,50}", chosen.name) is None:
        return None
    studio = next(item for item in studios if item.id == chosen.node_id)
    resolved = ResolvedImageSpec(
        spec=spec,
        seeds=tuple(expand_image_seeds(spec.seed, spec.n)),
        pipeline_version=IMAGE_RUNTIME_VERSION,
        environment_fingerprint=image_environment_fingerprint(studio, model.manifest_sha256),
        model_sha256=model.manifest_sha256,
        dependencies=dependencies,
        inputs=tuple(
            ImageInputDigest(
                input_id=item.input_id,
                sha256=item.sha256,
                width=item.width,
                height=item.height,
            )
            for item in input_manifests
        ),
        preset_id=row.preset_id,
        preset_revision=row.preset_revision,
        spec_hash=canonical_spec_hash(spec),
    )
    bound = snapshot.bind(resolved)
    instance = await session.scalar(
        select(ModelInstanceRow)
        .join(InstanceMemberRow, InstanceMemberRow.instance_id == ModelInstanceRow.id)
        .where(
            ModelInstanceRow.model_id == model.id,
            ModelInstanceRow.policy == f"image:{chosen.name}",
            InstanceMemberRow.node_id == chosen.node_id,
            ModelInstanceRow.state.in_(_REUSABLE_WORKER),
        )
        .limit(1)
    )
    if instance is None:
        instance = ModelInstanceRow(
            model_id=model.id,
            variant_id=None,
            policy=f"image:{chosen.name}",
            state=InstanceState.LAUNCHING,
            in_flight=0,
            created_at=current,
            updated_at=current,
            transitioned_at=current,
        )
        session.add(instance)
        await session.flush()
        session.add(
            InstanceMemberRow(
                instance_id=instance.id,
                node_id=chosen.node_id,
                rank=0,
                host=studio.control_host or studio.name,
                rank_healthy=False,
            )
        )
        await session.flush()
    existing_hold = await session.scalar(
        select(MemoryReservationRow)
        .where(
            MemoryReservationRow.node_id == chosen.node_id,
            MemoryReservationRow.holder_type == ReservationHolder.IMAGE,
            MemoryReservationRow.holder_id == str(instance.id),
        )
        .with_for_update()
    )
    if existing_hold is None:
        session.add(
            MemoryReservationRow(
                node_id=chosen.node_id,
                holder_type=ReservationHolder.IMAGE,
                holder_id=str(instance.id),
                bytes=reservation,
                pinned=False,
                state=MemoryReservationState.HELD,
                last_used_at=current,
            )
        )
    elif (
        existing_hold.state is not MemoryReservationState.HELD or existing_hold.bytes < reservation
    ):
        raise ImageConflict("image worker memory reservation differs from placement")
    else:
        existing_hold.last_used_at = current
    row.fence = 1
    row.selected_node_id = chosen.node_id
    row.instance_id = instance.id
    row.state = ImageJobState.RESERVING
    row.resolved_spec = bound.model_dump(mode="json")
    recorded = dict(row.authorization_snapshot)
    recorded["reservation_bytes"] = reservation
    row.authorization_snapshot = recorded
    row.updated_at = current
    row.version += 1
    await mark_image_job_started(session, row.owner_user_id, spec.n, now=current)
    session.add(
        ImageExecutionLeaseRow(
            node_id=chosen.node_id,
            job_id=row.id,
            mode="image",
            fence=row.fence,
            heartbeat_at=current,
            expires_at=max(row.deadline_at, current + timedelta(minutes=5)),
        )
    )
    await write_audit(
        session,
        actor="coire-scheduler",
        action="image.dispatch",
        target_type="image_job",
        target_id=row.id,
        outcome=AuditOutcome.OK,
        context={"node": chosen.name, "model_id": str(model.id)},
    )
    return _commands(
        row,
        chosen.name,
        instance.id,
        resolved,
        reservation,
        settings,
        model.slug,
        input_manifests,
    )
