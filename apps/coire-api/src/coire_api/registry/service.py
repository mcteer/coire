"""The registry's business logic.

`transition` is the only place `ModelRow.state` changes, and it always writes a reason. That
single choke point is what makes spec FR-002 true rather than aspirational — a state that
changed without a recorded reason is a state nobody can explain later.

The other rule that runs through this module: **`ready` is derived, never asserted**.
`recompute_state` re-establishes `ready ⇔ two verified copies` from the copy rows on every
pass, so a bug elsewhere degrades to "not ready" rather than to a model the gateway will route
to and the node cannot serve (spec FR-008, SC-003).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_audit
from coire_api.db import (
    DownloadJobRow,
    EngineProcessRow,
    ModelCopyRow,
    ModelRow,
    ModelStateTransitionRow,
    NodeRow,
)
from coire_api.nodes_client import NodeClient, NodeError, NodeErrorKind
from coire_api.registry.placement import (
    NoCandidate,
    NodeView,
    choose_origin,
    effective_reachability,
    replica_for,
)
from coire_core.memory import (
    NodeCapacity,
    estimate_bytes,
    fits_disk,
    fits_memory,
    precision_label,
)
from coire_core.models.audit import AuditAction, AuditOutcome
from coire_core.models.engine import LIVE_ENGINE_STATES, EngineState
from coire_core.models.jobs import DownloadStage, RepoInspection
from coire_core.models.registry import (
    CapabilityProfile,
    LoadState,
    ModelAddRequest,
    ModelListing,
    ModelRejected,
    ModelState,
    ModelUpdateRequest,
    RejectionReason,
    Tag,
    Visibility,
    slug_for,
)
from coire_core.settings import Settings

logger = logging.getLogger(__name__)


class RegistryError(Exception):
    """A registry operation was refused, with the shape the route should return."""

    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class Rejected(RegistryError):
    def __init__(self, rejection: ModelRejected) -> None:
        super().__init__(422, rejection.model_dump(mode="json"))
        self.rejection = rejection


# --------------------------------------------------------------------------- state machine


async def transition(
    session: AsyncSession, model: ModelRow, to_state: ModelState, reason: str
) -> None:
    """Move a model to a new state and record why.

    The only writer of `ModelRow.state`. A no-op transition still records nothing rather than
    filling the history with noise, but a *reason* change is worth keeping.
    """
    if model.state is to_state:
        if reason and model.state_reason != reason:
            model.state_reason = reason
            model.updated_at = datetime.now(UTC)
        return

    session.add(
        ModelStateTransitionRow(
            model_id=model.id, from_state=model.state, to_state=to_state, reason=reason
        )
    )
    logger.info("model %s: %s -> %s (%s)", model.slug, model.state.value, to_state.value, reason)
    model.state = to_state
    model.state_reason = None if to_state is ModelState.READY else reason
    model.updated_at = datetime.now(UTC)
    if to_state is ModelState.READY and model.ready_at is None:
        model.ready_at = datetime.now(UTC)


async def recompute_state(session: AsyncSession, model: ModelRow) -> ModelState:
    """Re-derive `ready` from the copies. Never trusts a previous pass.

    Deliberately one-directional out of `ready`: a model that lost a copy goes back to
    `replicating`, which is the truth, rather than staying `ready` because it once was.
    """
    if model.state in (ModelState.FAILED, ModelState.RETIRED):
        return model.state

    copies = (
        (await session.execute(select(ModelCopyRow).where(ModelCopyRow.model_id == model.id)))
        .scalars()
        .all()
    )
    verified = [c for c in copies if c.verified]

    if len(verified) >= 2:
        await transition(session, model, ModelState.READY, "two verified copies")
    elif verified:
        await transition(
            session,
            model,
            ModelState.REPLICATING,
            f"{len(verified)} of 2 copies verified",
        )
    return model.state


# --------------------------------------------------------------------------- node views


async def node_views(
    session: AsyncSession, statuses: dict[str, Any], settings: Settings
) -> list[NodeView]:
    """Registry rows joined to the last status the prober received."""
    rows = (await session.execute(select(NodeRow))).scalars().all()
    views: list[NodeView] = []
    for row in rows:
        status = statuses.get(row.name)
        views.append(
            NodeView(
                name=row.name,
                reachability=effective_reachability(
                    row.reachability,
                    row.last_observed_at or row.last_seen_at,
                    settings.node_health_freshness_s,
                ),
                store_free_bytes=getattr(status, "store_free_bytes", 0) or 0,
                memory_budget_bytes=getattr(status, "memory_budget_bytes", 0) or 0,
                memory_committed_bytes=getattr(status, "memory_committed_bytes", 0) or 0,
            )
        )
    return views


# --------------------------------------------------------------------------- add


async def add_model(
    session: AsyncSession,
    request: ModelAddRequest,
    *,
    client: NodeClient,
    settings: Settings,
    views: list[NodeView],
    actor: str,
) -> tuple[ModelRow, DownloadJobRow]:
    """Add a model, refusing before any bytes move if it cannot work (spec FR-010)."""
    slug = slug_for(request.repo_id)

    existing = (
        await session.execute(select(ModelRow).where(ModelRow.repo_id == request.repo_id))
    ).scalar_one_or_none()
    if existing is not None:
        await write_audit(
            session,
            actor=actor,
            action=AuditAction.MODEL_ADD,
            target_type="model",
            target_id=request.repo_id,
            outcome=AuditOutcome.REFUSED,
            detail={"reason": "duplicate", "existing_id": str(existing.id)},
        )
        raise RegistryError(409, f"{request.repo_id} is already in the registry")

    healthy = [v for v in views if v.healthy]
    if not healthy:
        raise RegistryError(503, "no node is reachable to inspect the repository")

    inspection = await _inspect(session, client, healthy, request.repo_id, actor)

    if not inspection.is_mlx_format:
        raise await _reject(
            session,
            actor,
            request.repo_id,
            ModelRejected(
                reason=RejectionReason.NOT_MLX_FORMAT,
                message=(
                    "this repository is not MLX-format. Feature 001 acquires only repositories "
                    "that need no conversion; inspection, conversion and validation are "
                    "feature 002's acquisition pipeline."
                    + (
                        " It is GGUF-only, which mlx-lm cannot load at all — look for the "
                        "original safetensors repository instead."
                        if inspection.has_gguf_only
                        else ""
                    )
                ),
                detail={
                    "architecture": inspection.architecture,
                    "gguf_only": inspection.has_gguf_only,
                },
            ),
        )

    precision = precision_label(inspection)
    estimate = estimate_bytes(
        inspection,
        overhead=settings.overhead_for(precision),
        kv_headroom_tokens=settings.kv_headroom_tokens,
    )

    capacities = [
        NodeCapacity(
            name=v.name,
            memory_budget_bytes=v.memory_budget_bytes,
            store_free_bytes=v.store_free_bytes,
            healthy=v.healthy,
        )
        for v in views
    ]
    memory_fit = fits_memory(estimate, capacities)
    if not memory_fit.ok:
        raise await _reject(
            session,
            actor,
            request.repo_id,
            ModelRejected(
                reason=RejectionReason.NO_FIT_MEMORY,
                message=(
                    f"an estimated {estimate} bytes fits no single node; sharded placement is "
                    "feature 006"
                ),
                required_bytes=estimate,
                available_bytes=memory_fit.available_bytes,
            ),
        )

    disk_fit = fits_disk(
        inspection.total_bytes, capacities, reserve_bytes=settings.disk_reserve_bytes
    )
    if not disk_fit.ok:
        raise await _reject(
            session,
            actor,
            request.repo_id,
            ModelRejected(
                reason=RejectionReason.NO_FIT_DISK,
                message=(
                    "every Studio must hold a copy, and at least one cannot: "
                    f"{disk_fit.required_bytes} bytes needed including the reserve, "
                    f"{disk_fit.available_bytes} free on the smallest"
                ),
                required_bytes=disk_fit.required_bytes,
                available_bytes=disk_fit.available_bytes,
            ),
        )

    try:
        origin = choose_origin(views)
        replica = replica_for(origin, views)
    except NoCandidate as exc:
        raise RegistryError(503, str(exc)) from exc

    model = ModelRow(
        id=uuid.uuid4(),
        repo_id=request.repo_id,
        slug=slug,
        display_name=request.display_name or request.repo_id.split("/", 1)[1],
        description=request.description,
        state=ModelState.DOWNLOADING,
        visibility=Visibility.ADMIN_ONLY,
        entitlement=[],
        tags=[t.value for t in request.tags],
        placement_policy=request.placement_policy,
        precision=precision,
        weight_bytes=inspection.weight_bytes,
        total_bytes=inspection.total_bytes,
        file_count=len(inspection.files),
        memory_estimate_bytes=estimate,
        idle_ttl_seconds=request.idle_ttl_seconds,
        context_window=inspection.max_position_embeddings,
        capability_profile=CapabilityProfile(
            context_window=inspection.max_position_embeddings,
            chat_template_present=inspection.chat_template_present,
        ).model_dump(mode="json"),
    )
    session.add(model)
    await session.flush()
    session.add(
        ModelStateTransitionRow(
            model_id=model.id,
            from_state=None,
            to_state=ModelState.DOWNLOADING,
            reason=f"added by {actor}",
        )
    )

    origin_row, replica_row = await _node_rows(session, origin.name, replica.name)
    job = DownloadJobRow(
        id=uuid.uuid4(),
        model_id=model.id,
        origin_node_id=origin_row.id,
        replica_node_id=replica_row.id,
        stage=DownloadStage.PULL,
        bytes_total=inspection.total_bytes,
        files_total=len(inspection.files),
    )
    session.add(job)

    await write_audit(
        session,
        actor=actor,
        action=AuditAction.MODEL_ADD,
        target_type="model",
        target_id=str(model.id),
        detail={
            "repo_id": request.repo_id,
            "origin": origin.name,
            "replica": replica.name,
            "estimate_bytes": estimate,
            "precision": precision,
        },
    )
    logger.info(
        "added %s (%s, %d bytes, estimate %d) pulling to %s",
        request.repo_id,
        precision,
        inspection.total_bytes,
        estimate,
        origin.name,
    )
    return model, job


async def _inspect(
    session: AsyncSession,
    client: NodeClient,
    healthy: list[NodeView],
    repo_id: str,
    actor: str,
) -> RepoInspection:
    """Inspect on any healthy node, trying the next when one is merely unreachable."""
    last: NodeError | None = None
    for view in healthy:
        try:
            return await client.inspect(view.name, repo_id)
        except NodeError as exc:
            last = exc
            if exc.kind is NodeErrorKind.GATED:
                raise await _reject(
                    session,
                    actor,
                    repo_id,
                    ModelRejected(
                        reason=RejectionReason.GATED,
                        message=(
                            "the repository is gated. Accept its licence on huggingface.co "
                            "with the account whose token the Studios hold, then add it again."
                        ),
                    ),
                ) from exc
            if exc.kind is NodeErrorKind.NOT_FOUND:
                raise await _reject(
                    session,
                    actor,
                    repo_id,
                    ModelRejected(
                        reason=RejectionReason.NOT_FOUND,
                        message="no such repository on Hugging Face",
                    ),
                ) from exc
            logger.warning("inspection on %s failed (%s); trying another node", view.name, exc)

    raise await _reject(
        session,
        actor,
        repo_id,
        ModelRejected(
            reason=RejectionReason.INSPECT_FAILED,
            message=f"no node could inspect the repository: {last}",
        ),
    )


async def _reject(
    session: AsyncSession, actor: str, repo_id: str, rejection: ModelRejected
) -> Rejected:
    await write_audit(
        session,
        actor=actor,
        action=AuditAction.MODEL_ADD,
        target_type="model",
        target_id=repo_id,
        outcome=AuditOutcome.REFUSED,
        detail={"reason": rejection.reason.value, "message": rejection.message},
    )
    return Rejected(rejection)


async def _node_rows(session: AsyncSession, *names: str) -> tuple[NodeRow, ...]:
    rows = (await session.execute(select(NodeRow).where(NodeRow.name.in_(names)))).scalars().all()
    by_name = {r.name: r for r in rows}
    missing = [n for n in names if n not in by_name]
    if missing:
        raise RegistryError(503, f"node(s) not registered: {', '.join(missing)}")
    return tuple(by_name[n] for n in names)


# --------------------------------------------------------------------------- retry / delete


async def retry_model(session: AsyncSession, model: ModelRow, *, actor: str) -> DownloadJobRow:
    """Re-run a failed acquisition from the earliest stage that still needs doing."""
    if model.state is not ModelState.FAILED:
        raise RegistryError(409, f"{model.slug} is {model.state.value}, not failed")

    job = (
        (
            await session.execute(
                select(DownloadJobRow)
                .where(DownloadJobRow.model_id == model.id)
                .order_by(DownloadJobRow.started_at.desc())
            )
        )
        .scalars()
        .first()
    )
    if job is None:
        raise RegistryError(409, "no acquisition job to retry")

    copies = (
        (await session.execute(select(ModelCopyRow).where(ModelCopyRow.model_id == model.id)))
        .scalars()
        .all()
    )
    verified = {c.node_id for c in copies if c.verified}
    # A new job id, because the node's verbs are idempotent on it: reusing the old one would
    # return the failed job rather than starting a fresh attempt.
    job.id = uuid.uuid4()
    job.stage = DownloadStage.EXPORT if job.origin_node_id in verified else DownloadStage.PULL
    job.attempt += 1
    job.failure_reason = None
    job.finished_at = None
    job.bytes_done = 0
    job.files_done = 0
    job.transfer_grant = None
    job.updated_at = datetime.now(UTC)

    for copy in copies:
        if not copy.verified:
            await session.delete(copy)

    await transition(
        session, model, ModelState.DOWNLOADING, f"retry {job.attempt} requested by {actor}"
    )
    await write_audit(
        session,
        actor=actor,
        action=AuditAction.MODEL_RETRY,
        target_type="model",
        target_id=str(model.id),
        detail={"attempt": job.attempt, "from_stage": job.stage.value},
    )
    return job


async def delete_model(
    session: AsyncSession, model: ModelRow, *, client: NodeClient, actor: str
) -> None:
    """Remove a failed model outright: rows and any partial copies."""
    if model.state is not ModelState.FAILED:
        raise RegistryError(
            409,
            f"{model.slug} is {model.state.value}; only a failed model can be deleted "
            "outright — retire a ready one instead",
        )
    nodes = (await session.execute(select(NodeRow))).scalars().all()
    for node in nodes:
        try:
            await client.delete_model(node.name, model.slug)
        except NodeError as exc:
            logger.warning("could not clean %s from %s: %s", model.slug, node.name, exc)
    await write_audit(
        session,
        actor=actor,
        action=AuditAction.MODEL_DELETE,
        target_type="model",
        target_id=str(model.id),
        detail={"repo_id": model.repo_id},
    )
    await session.delete(model)


# --------------------------------------------------------------------------- listing


def load_state_for(engines: list[EngineProcessRow]) -> tuple[LoadState, list[str]]:
    live = [e for e in engines if e.state in LIVE_ENGINE_STATES]
    ready = [e for e in live if e.state is EngineState.READY]
    if ready:
        return LoadState.LOADED, sorted({str(e.node_id) for e in ready})
    if live:
        return LoadState.LOADING, []
    return LoadState.COLD, []


def visible_to(*, is_admin: bool, model: ModelRow) -> bool:
    """Whether a caller may see a model at all.

    An admin sees everything. Anyone else sees only published, ready models — and, until
    feature 007 supplies real subjects, only those with an empty entitlement list, because
    there is nobody yet who could be on one.
    """
    if is_admin:
        return True
    return (
        model.visibility is Visibility.PUBLISHED
        and model.state is ModelState.READY
        and not model.entitlement
    )


def to_listing(
    model: ModelRow, engines: list[EngineProcessRow], *, node_names: dict[uuid.UUID, str]
) -> ModelListing:
    state, node_ids = load_state_for(engines)
    warmup = next(
        (
            e.load_seconds
            for e in sorted(engines, key=lambda e: e.started_at, reverse=True)
            if e.load_seconds
        ),
        None,
    )
    profile = model.capability_profile or {}
    return ModelListing(
        id=model.id,
        display_name=model.display_name,
        description=model.description,
        tags=[Tag(t) for t in (model.tags or [])],
        context_window=model.context_window,
        precision=model.precision,
        load_state=state,
        loaded_on=[node_names.get(uuid.UUID(n), n) for n in node_ids],
        estimated_warmup_seconds=warmup,
        capability_profile=CapabilityProfile.model_validate(profile),
    )


# --------------------------------------------------------------------------- curation


async def update_model(
    session: AsyncSession, model: ModelRow, request: ModelUpdateRequest, *, actor: str
) -> ModelRow:
    """Apply a curation change (spec US5)."""
    if model.state is ModelState.RETIRED:
        raise RegistryError(409, "a retired model cannot be edited")

    fields = request.model_fields_set
    changes: dict[str, Any] = {}

    if "visibility" in fields and request.visibility is not None:
        if request.visibility is Visibility.PUBLISHED and model.state is not ModelState.READY:
            raise RegistryError(
                409,
                f"{model.slug} is {model.state.value}; only a ready model can be published",
            )
        if request.visibility is not model.visibility:
            changes["visibility"] = request.visibility.value
            model.visibility = request.visibility

    for name in ("display_name", "description", "placement_policy", "idle_ttl_seconds"):
        if name in fields:
            value = getattr(request, name)
            if value is not None or name in ("description", "idle_ttl_seconds"):
                changes[name] = value
                setattr(model, name, value)

    if "tags" in fields and request.tags is not None:
        changes["tags"] = [t.value for t in request.tags]
        model.tags = changes["tags"]

    if "entitlement" in fields and request.entitlement is not None:
        changes["entitlement"] = request.entitlement
        model.entitlement = request.entitlement

    if "chat_template" in fields:
        # Stored only. It applies on the next load; engines already running keep the template
        # they started with, since drain-and-restart is feature 005's.
        changes["chat_template"] = "set" if request.chat_template else "cleared"
        model.chat_template = request.chat_template

    if "capability_profile" in fields and request.capability_profile is not None:
        profile = CapabilityProfile.model_validate(model.capability_profile or {})
        patch = request.capability_profile.model_dump(exclude_unset=True, exclude_none=True)
        merged = profile.model_copy(update=patch)
        model.capability_profile = merged.model_dump(mode="json")
        changes["capability_profile"] = patch

    model.updated_at = datetime.now(UTC)

    action = AuditAction.MODEL_UPDATE
    if "visibility" in changes:
        action = (
            AuditAction.MODEL_PUBLISH
            if model.visibility is Visibility.PUBLISHED
            else AuditAction.MODEL_UNPUBLISH
        )
    await write_audit(
        session,
        actor=actor,
        action=action,
        target_type="model",
        target_id=str(model.id),
        detail={"changes": changes},
    )
    return model


async def retire_model(session: AsyncSession, model: ModelRow, *, actor: str) -> ModelRow:
    """Retire: unload everywhere, delete both copies, keep the row for audit (US5).

    The state moves immediately and the cleanup is driven by the reconciler, so a node that is
    down when an admin retires a model does not leave the operation half-done — it is retried
    until every node confirms.
    """
    if model.state is ModelState.RETIRED:
        raise RegistryError(409, f"{model.slug} is already retired")
    await transition(session, model, ModelState.RETIRED, f"retired by {actor}")
    await write_audit(
        session,
        actor=actor,
        action=AuditAction.MODEL_RETIRE,
        target_type="model",
        target_id=str(model.id),
        detail={"repo_id": model.repo_id},
    )
    return model


__all__ = [
    "RegistryError",
    "Rejected",
    "add_model",
    "delete_model",
    "load_state_for",
    "node_views",
    "recompute_state",
    "retire_model",
    "retry_model",
    "to_listing",
    "transition",
    "update_model",
    "visible_to",
]
