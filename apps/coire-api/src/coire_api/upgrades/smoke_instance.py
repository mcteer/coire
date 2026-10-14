"""Durable private tiny instances use the normal scheduler and node lifecycle."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid5

from opentelemetry import trace
from sqlalchemy import func, select

from coire_api.auth import Principal
from coire_api.db import ModelInstanceRow, NodeRow, session_scope
from coire_api.instance.service import append_initial_transition, transition
from coire_api.upgrades.executor import scoped_operation
from coire_api.upgrades.service import smoke_asset
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.instance import InstanceState
from coire_core.models.upgrades import (
    CoreNodeSmokeScope,
    UpgradeExecutionPlan,
    UpgradeSmokeRequest,
    UpgradeSmokeResult,
)
from coire_core.settings import Settings

NAMESPACE = UUID("fe627caf-076c-484a-9b29-14209171c80b")
tracer = trace.get_tracer("coire.api.upgrade_smoke")


def smoke_instance_id(plan: UpgradeExecutionPlan, check: str, attempt: int = 0) -> UUID:
    return uuid5(
        NAMESPACE, f"{plan.operation.job_id}:{plan.operation.generation}:{check}:{attempt}"
    )


def verify_retained_result(scope: CoreNodeSmokeScope, result: UpgradeSmokeResult) -> None:
    plan = scope.plan
    release = plan.source if scope.check == "source" else plan.target
    if (
        (
            result.job_id,
            result.generation,
            result.check,
            result.runtime_sha256,
            result.model_id,
            result.model_variant_id,
            result.model_manifest_sha256,
        )
        != (
            plan.operation.job_id,
            plan.operation.generation,
            scope.check,
            release.manifest_sha256,
            plan.model_id,
            plan.model_variant_id,
            plan.model_manifest_sha256,
        )
        or not result.passed
        or not 10 <= result.generated_tokens <= 64
        or result.finished_at > scope.deadline_at
    ):
        raise UpgradeConflict("Core smoke result differs from original serving authority")


async def retained_smoke_result(
    plan: UpgradeExecutionPlan, body: UpgradeSmokeRequest, deadline: datetime
) -> UpgradeSmokeResult | None:
    async with session_scope() as session:
        for attempt in range(8):
            row = await session.get(ModelInstanceRow, smoke_instance_id(plan, body.check, attempt))
            if row is None or row.upgrade_smoke_result is None:
                continue
            scope = CoreNodeSmokeScope.model_validate(row.upgrade_smoke_payload)
            result = UpgradeSmokeResult.model_validate(row.upgrade_smoke_result)
            verify_retained_result(scope, result)
            if (
                scope.plan.authority_sha256() != plan.authority_sha256()
                or scope.deadline_at != deadline
            ):
                raise UpgradeConflict("Retained Core smoke result differs from original authority")
            if row.state is not InstanceState.STOPPED:
                raise UpgradeConflict("Retained Core smoke result lacks completed cleanup")
            if 0 <= (datetime.now(UTC) - result.finished_at).total_seconds() <= 5:
                return result
    return None


async def retain_smoke_result(
    identity: UUID, scope: CoreNodeSmokeScope, result: UpgradeSmokeResult
) -> None:
    verify_retained_result(scope, result)
    async with session_scope() as session:
        row = await session.get(ModelInstanceRow, identity, with_for_update=True)
        if (
            row is None
            or row.state is not InstanceState.STOPPED
            or row.upgrade_smoke_payload != scope.model_dump(mode="json")
        ):
            raise UpgradeConflict("Core smoke result lacks exact completed native cleanup")
        payload = result.model_dump(mode="json")
        if row.upgrade_smoke_result is not None and row.upgrade_smoke_result != payload:
            raise UpgradeConflict("Core smoke result is immutable")
        row.upgrade_smoke_result = payload


async def cleanup_prior_smokes(
    plan: UpgradeExecutionPlan, settings: Settings, deadline: datetime, *, check: str = "source"
) -> None:
    """Close this job's interrupted attempts before admitting another engine."""
    retained: list[tuple[UUID, CoreNodeSmokeScope]] = []
    async with session_scope() as session:
        for stage in ("gateway", "source") if check == "source" else ("gateway",):
            for attempt in range(8):
                identity = smoke_instance_id(plan, stage, attempt)
                row = await session.get(ModelInstanceRow, identity, populate_existing=True)
                if row is None or (
                    row.state is InstanceState.STOPPED and row.upgrade_smoke_result is not None
                ):
                    continue
                scope = CoreNodeSmokeScope.model_validate(row.upgrade_smoke_payload)
                if scope.plan.authority_sha256() != plan.authority_sha256() or scope.check != stage:
                    raise UpgradeConflict("Interrupted smoke changed its accepted owner")
                retained.append((identity, scope))
    for identity, scope in retained:
        await cleanup_smoke_instance(settings, identity, scope, deadline)


@tracer.start_as_current_span(
    "coire.api.upgrade_smoke_instance_prepare",
    record_exception=False,
    set_status_on_exception=False,
)
async def prepare_smoke_instance(
    plan: UpgradeExecutionPlan,
    body: UpgradeSmokeRequest,
    principal: Principal,
    settings: Settings,
    deadline: datetime,
) -> tuple[UUID, CoreNodeSmokeScope]:
    from coire_scheduler.image_admission import new_chat_mix_allowed

    identity = smoke_instance_id(plan, body.check)
    async with session_scope() as session:
        await session.execute(
            select(
                func.pg_advisory_xact_lock(func.hashtext(f"upgrade-smoke:{plan.operation.job_id}"))
            )
        )
        _, accepted = await scoped_operation(session, plan.operation.job_id, principal, settings)
        if accepted.authority_sha256() != plan.authority_sha256():
            raise UpgradeConflict("Core smoke instance differs from accepted authority")
        previous = None
        for attempt in range(8):
            identity = smoke_instance_id(plan, body.check, attempt)
            previous = await session.get(ModelInstanceRow, identity, populate_existing=True)
            if previous is None:
                break
            if previous.upgrade_smoke_payload is None:
                raise UpgradeConflict("Core smoke retry identity belongs to an ordinary instance")
            prior_scope = CoreNodeSmokeScope.model_validate(previous.upgrade_smoke_payload)
            if (
                prior_scope.plan.authority_sha256() != plan.authority_sha256()
                or prior_scope.deadline_at != deadline
                or prior_scope.check != body.check
            ):
                raise UpgradeConflict("Prior smoke attempt changed its original authority")
            if previous.state is not InstanceState.STOPPED:
                break
        else:
            raise UpgradeUnavailable(
                "Core smoke exhausted bounded attempts within its original budget"
            )
        if previous is not None:
            if previous.upgrade_smoke_payload is None:
                raise UpgradeConflict("Core smoke identity belongs to an ordinary instance")
            scope = CoreNodeSmokeScope.model_validate(previous.upgrade_smoke_payload)
            if (
                scope.plan.authority_sha256() != plan.authority_sha256()
                or scope.deadline_at != deadline
                or scope.check != body.check
            ):
                raise UpgradeConflict("Retained Core smoke instance preparation differs")
            if previous.state in {
                InstanceState.STOPPED,
                InstanceState.FAILED,
                InstanceState.DRAINING,
            }:
                raise UpgradeUnavailable(
                    "Prior Core smoke instance requires retained result or cleanup"
                )
        else:
            nodes = (
                await session.scalars(
                    select(NodeRow)
                    .where(NodeRow.name.in_(("coire-edge-a", "coire-edge-b")))
                    .order_by(NodeRow.name)
                )
            ).all()
            scope = None
            for node in nodes:
                if (
                    node.runtime_sha256 is None
                    or node.health_observed_at is None
                    or not 0
                    <= (datetime.now(UTC) - node.health_observed_at).total_seconds()
                    <= settings.placement_health_freshness_s
                ):
                    continue
                # Choose an eligible copy before pinning the private instance.
                # The ordinary scheduler rechecks this under its admission lock;
                # a standing image hold grants no unmeasured chat coexistence.
                if not await new_chat_mix_allowed(
                    session, node.id, plan.model_variant_id, datetime.now(UTC)
                ):
                    continue
                try:
                    asset = await smoke_asset(session, plan.model_id, (node.name,))
                except UpgradeUnavailable:
                    continue
                if asset != (
                    plan.model_id,
                    plan.model_variant_id,
                    plan.model_slug,
                    plan.model_manifest_sha256,
                ):
                    continue
                scope = CoreNodeSmokeScope(
                    plan=plan,
                    node="coire-edge-a" if node.name == "coire-edge-a" else "coire-edge-b",
                    engine_id=uuid5(identity, "engine"),
                    runtime_sha256=node.runtime_sha256,
                    deadline_at=deadline,
                    check="source" if body.check == "source" else "gateway",
                )
                break
            if scope is None:
                raise UpgradeUnavailable("Core smoke has no fresh verified tiny Studio copy")
            previous = ModelInstanceRow(
                id=identity,
                model_id=plan.model_id,
                variant_id=plan.model_variant_id,
                policy=f"pinned:{scope.node}",
                state=InstanceState.REQUESTED,
                upgrade_smoke_payload=scope.model_dump(mode="json"),
            )
            session.add(previous)
            await session.flush()
            await append_initial_transition(session, previous)
    if scope is None:
        raise UpgradeUnavailable("Core smoke preparation has no exact private scope")
    return identity, scope


async def wait_smoke_instance(settings: Settings, identity: UUID, deadline: datetime) -> None:
    while datetime.now(UTC) < deadline:
        async with session_scope() as session:
            instance = await session.get(ModelInstanceRow, identity, populate_existing=True)
            if instance is None or instance.upgrade_smoke_payload is None:
                raise UpgradeConflict("Core smoke lost its durable private instance")
            if instance.state is InstanceState.READY:
                return
            if instance.state in {
                InstanceState.FAILED,
                InstanceState.STOPPED,
                InstanceState.DRAINING,
            }:
                raise UpgradeUnavailable("Core smoke instance did not become ready")
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            break
        await asyncio.sleep(min(settings.placement_poll_interval_s, remaining))
    raise UpgradeUnavailable("Core smoke startup exceeded its original deadline")


@tracer.start_as_current_span(
    "coire.api.upgrade_smoke_instance_cleanup",
    record_exception=False,
    set_status_on_exception=False,
)
async def cleanup_smoke_instance(
    settings: Settings, identity: UUID, scope: CoreNodeSmokeScope, deadline: datetime
) -> None:
    from coire_api.db import EngineProcessRow, MemoryReservationRow, PlacementCommandRow
    from coire_api.nodes_client import NodeClient, NodeError
    from coire_api.placement.service import lock_nodes_for_admission
    from coire_core.models.engine import EngineState
    from coire_core.models.placement import MemoryReservationState, ReservationHolder

    # Serialize against the ordinary planner, then prevent pending commands from
    # acquiring another process. The node tombstone handles already dispatched
    # requests before any positive absence can release their memory obligations.
    async with session_scope() as session:
        instance = await session.get(ModelInstanceRow, identity, populate_existing=True)
        if instance is None or instance.upgrade_smoke_payload != scope.model_dump(mode="json"):
            raise UpgradeConflict("Core smoke cleanup lost its exact durable instance")
        node = await session.scalar(select(NodeRow).where(NodeRow.name == scope.node))
        if node is None:
            raise UpgradeUnavailable("Core smoke cleanup lost its Studio")
        await lock_nodes_for_admission(session, [node.id])
        instance = await session.get(
            ModelInstanceRow, identity, populate_existing=True, with_for_update=True
        )
        if instance is None or instance.upgrade_smoke_payload != scope.model_dump(mode="json"):
            raise UpgradeConflict("Core smoke cleanup changed its durable owner")
        if instance.state is InstanceState.READY:
            await transition(
                session, identity, InstanceState.DRAINING, reason="owned upgrade smoke cleanup"
            )
        elif instance.state not in {
            InstanceState.DRAINING,
            InstanceState.FAILED,
            InstanceState.STOPPED,
        }:
            await transition(
                session,
                identity,
                InstanceState.FAILED,
                reason="owned upgrade smoke startup closed",
                failure_code="upgrade_smoke_cleanup",
            )
        if instance.placement_decision_id is not None:
            commands = (
                await session.scalars(
                    select(PlacementCommandRow)
                    .where(
                        PlacementCommandRow.decision_id == instance.placement_decision_id,
                        PlacementCommandRow.operation == "load",
                        PlacementCommandRow.state == "pending",
                    )
                    .with_for_update()
                )
            ).all()
            for command in commands:
                command.state = "failed"
                command.failure_code = "upgrade_smoke_cleanup"
                command.failure_detail = "owned Core smoke startup was closed"
    async with NodeClient(settings, timeout=5) as client:
        while True:
            try:
                await client.cleanup_core_smoke(scope)
                break
            except NodeError as error:
                if error.status != 503 or datetime.now(UTC) >= deadline:
                    raise UpgradeUnavailable(
                        "Core smoke native cleanup is not positively verified"
                    ) from None
            remaining = (deadline - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                raise UpgradeUnavailable("Core smoke cleanup exceeded its original budget")
            await asyncio.sleep(min(settings.upgrade_poll_s, remaining))
    async with session_scope() as session:
        node = await session.scalar(select(NodeRow).where(NodeRow.name == scope.node))
        if node is None:
            raise UpgradeUnavailable("Core smoke cleanup lost its Studio ledger")
        await lock_nodes_for_admission(session, [node.id])
        instance = await session.get(
            ModelInstanceRow, identity, populate_existing=True, with_for_update=True
        )
        engine = await session.get(
            EngineProcessRow, scope.engine_id, populate_existing=True, with_for_update=True
        )
        if instance is None or instance.upgrade_smoke_payload != scope.model_dump(mode="json"):
            raise UpgradeConflict("Core smoke private instance changed during cleanup")
        if engine is not None:
            if (
                engine.instance_id,
                engine.model_id,
                engine.variant_id,
                engine.adapter_id,
                engine.node_id,
            ) != (identity, scope.plan.model_id, scope.plan.model_variant_id, None, node.id):
                raise UpgradeConflict("Core smoke cleanup found another engine ledger owner")
            engine.state = EngineState.STOPPED
            engine.stopped_at = datetime.now(UTC)
        reservations = (
            await session.scalars(
                select(MemoryReservationRow)
                .where(
                    MemoryReservationRow.holder_type == ReservationHolder.MODEL,
                    MemoryReservationRow.holder_id == str(identity),
                )
                .with_for_update()
            )
        ).all()
        for reservation in reservations:
            if reservation.node_id != node.id:
                raise UpgradeConflict("Core smoke cleanup found another Studio reservation")
            reservation.state = MemoryReservationState.RELEASED
            reservation.released_at = datetime.now(UTC)
        if instance.state is InstanceState.DRAINING:
            await transition(
                session,
                identity,
                InstanceState.STOPPED,
                reason="owned upgrade smoke native absence verified",
            )
        elif instance.state is InstanceState.FAILED:
            from coire_api.audit import write_audit
            from coire_api.db import InstanceTransitionRow

            # This private terminal close follows the exact positive native
            # receipt above. It does not widen ordinary failed-instance transitions.
            sequence = await session.scalar(
                select(func.coalesce(func.max(InstanceTransitionRow.sequence), 0)).where(
                    InstanceTransitionRow.instance_id == identity
                )
            )
            now = datetime.now(UTC)
            session.add(
                InstanceTransitionRow(
                    instance_id=identity,
                    sequence=int(sequence or 0) + 1,
                    previous_state=InstanceState.FAILED,
                    state=InstanceState.STOPPED,
                    reason="owned upgrade smoke native absence verified",
                    at=now,
                )
            )
            instance.state = InstanceState.STOPPED
            instance.transitioned_at = instance.updated_at = now
            await write_audit(
                session,
                actor="coire-upgrade-executor",
                action="instance.transition",
                target_type="model_instance",
                target_id=str(identity),
                detail={
                    "previous_state": "failed",
                    "state": "stopped",
                    "job_id": scope.plan.operation.job_id,
                    "reason": "owned upgrade smoke native absence verified",
                },
            )
