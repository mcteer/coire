"""Fixed reversible ops action registry backed by existing domain services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api import runs
from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import (
    AgentRunRow,
    MemoryReservationRow,
    ModelInstanceRow,
    ModelRow,
    ModelVariantRow,
    PlacementDecisionRow,
    RunCommandRow,
)
from coire_api.instance import service as instance_service
from coire_api.placement import service as placement_service
from coire_api.run_tokens import revoke_run_token
from coire_core.models.instance import InstanceState
from coire_core.models.ops import (
    InstanceLoadAction,
    InstanceUnloadAction,
    ModelPinAction,
    ModelUnpinAction,
    ResolvedOpsAction,
    RunKillAction,
)
from coire_core.models.placement import (
    MemoryReservationState,
    PinUpdate,
    PlacementState,
    ReservationHolder,
)
from coire_core.models.registry import ModelState
from coire_core.models.runs import (
    TERMINAL_RUN_STATES,
    AgentRunState,
    RunCommandState,
    RunOperation,
)
from coire_core.settings import Settings


class OpsActionError(RuntimeError):
    def __init__(self, code: str, detail: str, *, stale: bool = False) -> None:
        self.code = code
        self.detail = detail
        self.stale = stale
        super().__init__(detail)


@dataclass(frozen=True)
class ActionSpec:
    operation: str
    target_type: str
    action_type: (
        type[InstanceUnloadAction]
        | type[RunKillAction]
        | type[ModelPinAction]
        | type[ModelUnpinAction]
        | type[InstanceLoadAction]
    )
    reversible: bool = True


def resource_version(updated_at: datetime) -> str:
    return updated_at.isoformat()


def _check_precondition(action: ResolvedOpsAction, *, state: str, updated_at: datetime) -> None:
    if action.precondition.expected_state != state:
        raise OpsActionError(
            "state_changed",
            "resource state changed after proposal",
            stale=True,
        )
    if action.precondition.resource_version != resource_version(updated_at):
        raise OpsActionError(
            "version_changed",
            "resource version changed after proposal",
            stale=True,
        )


async def _unload_instance(
    session: AsyncSession,
    action: InstanceUnloadAction,
    principal: Principal,
    settings: Settings,
) -> dict[str, object]:
    row = await session.scalar(
        select(ModelInstanceRow).where(ModelInstanceRow.id == action.target_id).with_for_update()
    )
    if row is None:
        raise OpsActionError("target_missing", "model instance no longer exists", stale=True)
    _check_precondition(action, state=row.state.value, updated_at=row.updated_at)
    if row.state is not InstanceState.READY or row.in_flight:
        raise OpsActionError("instance_busy", "instance is not ready and idle", stale=True)
    row.drain_deadline = datetime.now(UTC) + timedelta(seconds=settings.instance_drain_timeout_s)
    row = await instance_service.transition(
        session,
        row.id,
        InstanceState.DRAINING,
        reason="confirmed ops unload",
    )
    return {"instance_id": str(row.id), "state": row.state.value}


async def _kill_run(
    session: AsyncSession,
    action: RunKillAction,
    principal: Principal,
    settings: Settings,
) -> dict[str, object]:
    row = await session.scalar(
        select(AgentRunRow).where(AgentRunRow.id == action.target_id).with_for_update()
    )
    if row is None:
        raise OpsActionError("target_missing", "agent run no longer exists", stale=True)
    _check_precondition(action, state=row.state.value, updated_at=row.updated_at)
    if row.state in TERMINAL_RUN_STATES or row.state is AgentRunState.KILL_REQUESTED:
        raise OpsActionError("run_inactive", "agent run is no longer active", stale=True)
    await revoke_run_token(session, row.id)
    await runs.transition(session, row, AgentRunState.KILL_REQUESTED, "confirmed ops kill")
    if row.node_id is not None:
        command_id = runs.run_command_id(row.id, RunOperation.KILL)
        if await session.get(RunCommandRow, command_id) is None:
            session.add(
                RunCommandRow(
                    id=command_id,
                    run_id=row.id,
                    node_id=row.node_id,
                    operation=RunOperation.KILL,
                    attempt=1,
                    state=RunCommandState.PENDING,
                    detail={},
                )
            )
    row.killed_by = principal.user_id
    row.killed_at = datetime.now(UTC)
    return {"run_id": str(row.id), "state": row.state.value}


async def _set_model_pin(
    session: AsyncSession,
    action: ModelPinAction | ModelUnpinAction,
    principal: Principal,
    *,
    pinned: bool,
) -> dict[str, object]:
    model = await session.scalar(
        select(ModelRow).where(ModelRow.id == action.target_id).with_for_update()
    )
    if model is None:
        raise OpsActionError("target_missing", "model no longer exists", stale=True)
    _check_precondition(action, state=model.state.value, updated_at=model.updated_at)
    reservations = list(
        (
            await session.scalars(
                select(MemoryReservationRow)
                .where(
                    MemoryReservationRow.holder_type == ReservationHolder.MODEL,
                    MemoryReservationRow.holder_id == str(model.id),
                    MemoryReservationRow.state.in_(
                        (MemoryReservationState.PENDING, MemoryReservationState.HELD)
                    ),
                )
                .with_for_update()
            )
        ).all()
    )
    if not reservations:
        raise OpsActionError("reservation_missing", "model has no active reservation", stale=True)
    for reservation in reservations:
        await placement_service.set_pin(
            session,
            reservation.id,
            PinUpdate(pinned=pinned),
            actor=principal.subject or "admin",
        )
    return {
        "model_id": str(model.id),
        "pinned": pinned,
        "reservation_ids": [str(row.id) for row in reservations],
    }


async def _pin_model(
    session: AsyncSession,
    action: ModelPinAction,
    principal: Principal,
    settings: Settings,
) -> dict[str, object]:
    return await _set_model_pin(session, action, principal, pinned=True)


async def _unpin_model(
    session: AsyncSession,
    action: ModelUnpinAction,
    principal: Principal,
    settings: Settings,
) -> dict[str, object]:
    return await _set_model_pin(session, action, principal, pinned=False)


async def _load_instance(
    session: AsyncSession,
    action: InstanceLoadAction,
    principal: Principal,
    settings: Settings,
) -> dict[str, object]:
    model = await session.scalar(
        select(ModelRow).where(ModelRow.id == action.target_id).with_for_update()
    )
    if model is None:
        raise OpsActionError("target_missing", "model no longer exists", stale=True)
    _check_precondition(action, state=model.state.value, updated_at=model.updated_at)
    if model.state is not ModelState.READY:
        raise OpsActionError("model_not_ready", "model is no longer ready", stale=True)
    variant = await session.get(ModelVariantRow, action.parameters.variant_id)
    if variant is None or variant.model_id != model.id or not variant.validated:
        raise OpsActionError(
            "variant_unverified", "variant is not verified for this model", stale=True
        )
    decision = PlacementDecisionRow(
        model_id=model.id,
        variant_id=variant.id,
        policy=action.parameters.policy or model.placement_policy,
        required_bytes=max(1, variant.memory_estimate_bytes),
        state=PlacementState.REQUESTED,
    )
    session.add(decision)
    await session.flush()
    return {
        "model_id": str(model.id),
        "placement_decision_id": str(decision.id),
        "state": decision.state.value,
    }


ACTION_REGISTRY: dict[str, ActionSpec] = {
    "instance.unload": ActionSpec("instance.unload", "instance", InstanceUnloadAction),
    "run.kill": ActionSpec("run.kill", "run", RunKillAction),
    "model.pin": ActionSpec("model.pin", "model", ModelPinAction),
    "model.unpin": ActionSpec("model.unpin", "model", ModelUnpinAction),
    "instance.load": ActionSpec("instance.load", "model", InstanceLoadAction),
}


async def execute_action(
    session: AsyncSession,
    action: ResolvedOpsAction,
    principal: Principal,
    settings: Settings,
) -> dict[str, object]:
    spec = ACTION_REGISTRY.get(action.operation)
    if spec is None or not isinstance(action, spec.action_type):
        raise OpsActionError("operation_forbidden", "operation is not allowlisted")
    if action.target_type != spec.target_type or not spec.reversible:
        raise OpsActionError("operation_forbidden", "operation is not reversible")
    if isinstance(action, InstanceUnloadAction):
        result = await _unload_instance(session, action, principal, settings)
    elif isinstance(action, RunKillAction):
        result = await _kill_run(session, action, principal, settings)
    elif isinstance(action, ModelPinAction):
        result = await _pin_model(session, action, principal, settings)
    elif isinstance(action, ModelUnpinAction):
        result = await _unpin_model(session, action, principal, settings)
    else:
        result = await _load_instance(session, action, principal, settings)
    await write_principal_audit(
        session,
        principal=principal,
        action="ops.action.dispatch",
        target_type=action.target_type,
        target_id=str(action.target_id),
        detail={"operation": action.operation, "result": result},
        context={"proposer": "coire-ops"},
    )
    return result
