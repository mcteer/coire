"""DBOS coordinates accepted Studio authority; the independent node guard owns effects."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dbos import DBOS
from opentelemetry import metrics, trace
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import UpgradeMaintenanceRow, session_scope
from coire_api.nodes_client import NodeClient, NodeError, NodeErrorKind
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.grants import consume, initiating_actor, issue
from coire_api.upgrades.host_hold import operation_hold
from coire_api.upgrades.persistence import get_operation, operation_lock
from coire_api.upgrades.service import ingest_receipt
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import (
    NODE_UPGRADE_OWNERS,
    TERMINAL_UPGRADE_PHASES,
    DeploymentOwnership,
    DeploymentSelectionReceipt,
    NodeMaintenanceCommand,
    NodeUpgradeCancel,
    NodeUpgradeCommand,
    NodeUpgradeInventory,
    NodeUpgradeStatus,
    UpgradeActivationRequest,
    UpgradeExecutionPlan,
    UpgradeKind,
    UpgradeMaintenance,
    UpgradeOperation,
    UpgradePhase,
    UpgradeReceipt,
)
from coire_core.settings import Settings, get_settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_scheduler.maintenance import fence, release_verified

tracer = trace.get_tracer("coire.scheduler.upgrades")
logger = logging.getLogger(__name__)
ticks = metrics.get_meter("coire.scheduler.upgrades").create_counter(
    "coire_upgrade_reconciliation_ticks_total"
)
COORDINATOR = Principal(kind=PrincipalKind.SERVICE, subject="upgrade-coordinator")
NODE_OWNERS = NODE_UPGRADE_OWNERS


def verify_core_selection(settings: Settings, plan: UpgradeExecutionPlan) -> None:
    """Database terminal progress alone cannot reopen a physical runtime fence."""
    store = PrivateUpgradeStore(Path(settings.upgrade_coordination_dir))
    owner = store.read("ownership.json", DeploymentOwnership)
    selection = store.read("selection.json", DeploymentSelectionReceipt)
    target = plan.target if plan.operation.phase is UpgradePhase.SUCCEEDED else plan.source
    if (
        owner is None
        or selection is None
        or owner.state != "committed"
        or owner.stage != "selected"
        or owner.owner_kind != "executor"
        or (owner.job_id, owner.operation_generation)
        != (plan.operation.job_id, plan.operation.generation)
        or owner.project != settings.upgrade_project
        or (
            selection.project,
            selection.owner_identity,
            selection.boot_identity,
            selection.generation,
        )
        != (owner.project, owner.owner_identity, owner.boot_identity, owner.generation)
        or owner.committed_sha256 != target.manifest.compose_sha256
        or (selection.managed_release_id, selection.manifest_sha256)
        != (target.release_id, target.manifest_sha256)
        or (selection.release_key, selection.compose_sha256)
        != (target.manifest.host_release_key, target.manifest.compose_sha256)
    ):
        raise UpgradeConflict("Core terminal progress has no matching committed runtime selection")


async def reconcile_core_operation(job_id: str, settings: Settings) -> bool:
    """Coordinate durable admissions; independent executor exclusively owns Core effects."""
    if not settings.upgrades_enabled:
        return True
    async with session_scope() as session:
        row = await get_operation(session, job_id)
        operation = UpgradeOperation.model_validate(row.payload)
        if operation.kind is not UpgradeKind.CONTROL_PLANE or row.execution_plan is None:
            raise UpgradeUnavailable("Core coordination requires an accepted execution plan")
        plan = UpgradeExecutionPlan.model_validate(row.execution_plan)
        plan = plan.model_copy(update={"operation": operation})
    ticks.add(1, {"kind": "control_plane", "phase": operation.phase.value})
    async with NodeClient(settings, timeout=5) as client:
        if operation.finished_at is not None:
            await asyncio.to_thread(verify_core_selection, settings, plan)
            for target in plan.maintenance.targets:
                if target in {"coire-edge-a", "coire-edge-b"}:
                    await release_core_peer_maintenance(client, target, plan)
            async with session_scope() as session:
                await release_verified(session, job_id, operation.generation)
                await asyncio.to_thread(operation_hold, settings, plan, release=True)
            return True
        acknowledgments: dict[str, int] = {}
        async with session_scope() as session:
            established = True
            for target in plan.maintenance.targets:
                barrier = await session.get(UpgradeMaintenanceRow, target)
                if barrier is None:
                    established = False
                    continue
                value = UpgradeMaintenance.model_validate(barrier.payload)
                if barrier.released and value.generation < operation.generation:
                    established = False
                    continue
                if barrier.released or (value.job_id, value.generation) != (
                    operation.job_id,
                    operation.generation,
                ):
                    raise UpgradeConflict(
                        "Core reconciliation encountered another maintenance owner"
                    )
                established &= value.state == "active"
            if not established:
                await fence(session, plan.maintenance)
        for target in plan.maintenance.targets:
            if target in {"coire-edge-a", "coire-edge-b"}:
                ack = await client.apply_upgrade_maintenance(
                    target, NodeMaintenanceCommand(maintenance=plan.maintenance)
                )
                if ack.released or ack.generation != operation.generation:
                    raise UpgradeConflict("Core peer maintenance acknowledgment differs")
                acknowledgments[target] = ack.generation
        async with session_scope() as session:
            await fence(
                session,
                plan.maintenance.model_copy(
                    update={"state": "active", "acknowledged_generations": acknowledgments}
                ),
            )
        for target in plan.maintenance.targets:
            if target in {"coire-edge-a", "coire-edge-b"}:
                await client.apply_upgrade_maintenance(
                    target,
                    NodeMaintenanceCommand(maintenance=plan.maintenance, action="reconcile"),
                )
    # Strict Core and Studio idle inventories are collected by the API at each
    # activation boundary. Scheduler must never fabricate API-local ASGI state.
    return False


async def release_core_peer_maintenance(
    client: NodeClient, target: str, plan: UpgradeExecutionPlan
) -> None:
    """Close a partial Core fence under its original epoch after verified selection.

    A failure before fencing can leave a peer with no matching maintenance
    record. Establish the retained epoch without draining or stopping work,
    then require the node's normal independent census and terminal checks.
    Foreign or newer owners still reject establishment; released matching
    epochs return their existing acknowledgment without being reactivated.
    """
    if (
        plan.operation.kind is not UpgradeKind.CONTROL_PLANE
        or plan.operation.phase not in TERMINAL_UPGRADE_PHASES
        or plan.operation.finished_at is None
        or target not in {"coire-edge-a", "coire-edge-b"}
        or target not in plan.maintenance.targets
        or "core" not in plan.maintenance.targets
    ):
        raise UpgradeConflict("Core peer closure requires a verified terminal operation scope")
    release = NodeMaintenanceCommand(
        maintenance=plan.maintenance, action="release", terminal_verified=True
    )
    try:
        await client.apply_upgrade_maintenance(target, release)
    except NodeError as error:
        if error.kind is not NodeErrorKind.CONFLICT:
            raise
        await client.apply_upgrade_maintenance(
            target,
            NodeMaintenanceCommand(
                maintenance=plan.maintenance.model_copy(update={"state": "recovering"})
            ),
        )
        await client.apply_upgrade_maintenance(target, release)


async def reconcile_receipts(
    session: AsyncSession, plan: UpgradeExecutionPlan, status: NodeUpgradeStatus
) -> None:
    if (status.node, status.job_id, status.generation) != (
        plan.operation.node,
        plan.operation.job_id,
        plan.operation.generation,
    ):
        raise UpgradeConflict("Node progress belongs to another accepted owner")
    for receipt in status.receipts:
        await ingest_receipt(session, receipt, COORDINATOR)


def idle_inventory(
    plan: UpgradeExecutionPlan, snapshot: NodeUpgradeInventory, runtime: str
) -> bool:
    if (
        (snapshot.job_id, snapshot.generation, snapshot.runtime_sha256)
        != (plan.operation.job_id, plan.operation.generation, runtime)
        or snapshot.node not in plan.maintenance.targets
        or not 0 <= (datetime.now(UTC) - snapshot.observed_at).total_seconds() <= 5
    ):
        raise UpgradeConflict("Node drain observation differs from accepted ownership")
    return snapshot.idle(NODE_OWNERS)


async def activation_command(
    session: AsyncSession, plan: UpgradeExecutionPlan, peer: NodeUpgradeInventory | None
) -> NodeUpgradeCommand:
    """Save consumed intent and its exact peer snapshot before delivering any grant."""
    await operation_lock(session)
    row = await get_operation(session, plan.operation.job_id, lock=True)
    operation = UpgradeOperation.model_validate(row.payload)
    if row.node_command is not None:
        saved = NodeUpgradeCommand.model_validate(row.node_command)
        if saved.authority_sha256() != plan.authority_sha256() or saved.activation_grant is None:
            raise UpgradeConflict("Saved node activation command differs")
        return saved
    grant = await issue(
        session,
        operation.job_id,
        UpgradeActivationRequest(
            generation=operation.generation,
            source_sha256=operation.source_sha256,
        ),
    )
    receipt = UpgradeReceipt(
        job_id=operation.job_id,
        generation=operation.generation,
        sequence=operation.receipt_sequence + 1,
        phase=UpgradePhase.ACTIVATING,
        effect_id="runtime-activation",
        occurred_at=datetime.now(UTC),
        outcome="intent",
        observed_sha256=operation.source_sha256,
    )
    command = NodeUpgradeCommand.model_validate(
        plan.model_dump(mode="json")
        | {
            "authorization_expires_at": grant.expires_at,
            "activation_grant": grant,
            "activation_receipt": receipt,
            "peer_inventory": peer.model_dump(mode="json") if peer is not None else None,
        }
    )
    await consume(session, grant, receipt, COORDINATOR)
    row.node_command = command.model_dump(mode="json")
    await session.flush()
    return command


async def reconcile_node_operation(job_id: str, settings: Settings) -> bool:
    """Advance one persisted operation without granting time-based admission."""
    if not settings.upgrades_enabled:
        return True
    async with session_scope() as session:
        row = await get_operation(session, job_id)
        operation = UpgradeOperation.model_validate(row.payload)
        if operation.kind is not UpgradeKind.STUDIO or row.execution_plan is None:
            raise UpgradeUnavailable("Studio coordination requires an accepted execution plan")
        plan = UpgradeExecutionPlan.model_validate(row.execution_plan)
        saved = NodeUpgradeCommand.model_validate(row.node_command) if row.node_command else None
    node = operation.node
    if node is None:
        raise UpgradeConflict("Studio operation has no target")
    ticks.add(1, {"kind": "studio", "phase": operation.phase.value})
    async with NodeClient(settings, timeout=5) as client:
        if operation.finished_at is not None:
            unchanged = operation.phase in {
                UpgradePhase.FAILED_UNCHANGED,
                UpgradePhase.CANCELLED_UNCHANGED,
            }
            for target in plan.maintenance.targets:
                if target not in {"coire-edge-a", "coire-edge-b"}:
                    raise UpgradeConflict("Studio release includes another service scope")
                runtime = (
                    plan.source.manifest.environment_sha256
                    if target == node
                    else plan.peer_runtime_sha256
                )
                await client.apply_upgrade_maintenance(
                    target,
                    NodeMaintenanceCommand(
                        maintenance=plan.maintenance,
                        action="release",
                        terminal_verified=True,
                        source_unchanged=unchanged,
                        source_runtime_sha256=runtime if unchanged else None,
                    ),
                )
            async with session_scope() as session:
                await release_verified(session, job_id, operation.generation)
                await asyncio.to_thread(operation_hold, settings, plan, release=True)
            return True
        if operation.phase is UpgradePhase.ACCEPTED:
            await asyncio.to_thread(operation_hold, settings, plan)
            async with session_scope() as session:
                await fence(session, plan.maintenance)
            acknowledgments: dict[str, int] = {}
            for target in plan.maintenance.targets:
                if target not in {"coire-edge-a", "coire-edge-b"}:
                    raise UpgradeConflict("Studio fence includes another service scope")
                ack = await client.apply_upgrade_maintenance(
                    target, NodeMaintenanceCommand(maintenance=plan.maintenance)
                )
                if ack.released:
                    raise UpgradeConflict("Node has already released accepted maintenance")
                acknowledgments[target] = ack.generation
            async with session_scope() as session:
                await fence(
                    session,
                    plan.maintenance.model_copy(
                        update={
                            "state": "active",
                            "acknowledged_generations": acknowledgments,
                        }
                    ),
                )
        if saved is None and operation.phase is UpgradePhase.ACCEPTED:
            async with session_scope() as session:
                await operation_lock(session)
                row = await get_operation(session, job_id, lock=True)
                latest_operation = UpgradeOperation.model_validate(row.payload)
                if not latest_operation.cancel_requested:
                    await authorize(session, initiating_actor(row))
                command = NodeUpgradeCommand.model_validate(
                    plan.model_dump(mode="json")
                    | {
                        "authorization_expires_at": datetime.now(UTC) + timedelta(seconds=30),
                    }
                )
            if latest_operation.cancel_requested:
                status = await client.cancel_upgrade(
                    node,
                    job_id,
                    NodeUpgradeCancel(
                        generation=operation.generation,
                        unstarted_plan=plan,
                    ),
                )
            else:
                status = await client.execute_upgrade(node, command)
        else:
            status = await client.upgrade_status(
                node, job_id, after_sequence=operation.receipt_sequence
            )
        async with session_scope() as session:
            await reconcile_receipts(session, plan, status)
            row = await get_operation(session, job_id)
            current = UpgradeOperation.model_validate(row.payload)
        if current.finished_at is not None:
            return False  # Release requires the next pass's durable terminal observation.
        if current.cancel_requested:
            await client.cancel_upgrade(
                node,
                job_id,
                NodeUpgradeCancel(
                    generation=operation.generation,
                    reason="authorization_revoked"
                    if current.reason == "authorization_revoked"
                    else "administrator_cancelled",
                ),
            )
            return False
        if saved is not None:
            await client.execute_upgrade(node, saved)
            return False
        if current.phase is not UpgradePhase.SMOKE_BEFORE:
            for target in plan.maintenance.targets:
                await client.apply_upgrade_maintenance(
                    target,
                    NodeMaintenanceCommand(
                        maintenance=plan.maintenance,
                        action="reconcile",
                    ),
                )
            return False
        peer = None
        for target in plan.maintenance.targets:
            runtime = (
                plan.source.manifest.environment_sha256
                if target == node
                else plan.peer_runtime_sha256
            )
            if runtime is None:
                raise UpgradeConflict("Fenced Studio has no captured runtime identity")
            snapshot = await client.upgrade_inventory(target, job_id, operation.generation)
            if not idle_inventory(plan, snapshot, runtime):
                return False
            if target != node:
                peer = snapshot
        async with session_scope() as session:
            command = await activation_command(session, plan, peer)
        await client.execute_upgrade(node, command)
        return False


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=1)
async def reconcile_upgrade_step(job_id: str) -> bool:
    with tracer.start_as_current_span(
        "coire.scheduler.upgrade_reconcile", record_exception=False, set_status_on_exception=False
    ):
        settings = get_settings()
        try:
            async with session_scope() as session:
                row = await get_operation(session, job_id)
                kind = UpgradeOperation.model_validate(row.payload).kind
            if kind is UpgradeKind.CONTROL_PLANE:
                return await reconcile_core_operation(job_id, settings)
            return await reconcile_node_operation(job_id, settings)
        except UpgradeForbidden:
            # Revocation forbids a new activation, while the already accepted
            # node owner retains authority to unwind its own preparation.
            try:
                async with session_scope() as session:
                    row = await get_operation(session, job_id)
                    operation = UpgradeOperation.model_validate(row.payload)
                    retained_plan = (
                        UpgradeExecutionPlan.model_validate(row.execution_plan)
                        if row.execution_plan is not None
                        else None
                    )
                if operation.node is None:
                    raise UpgradeConflict("Revoked Studio operation has no target") from None
                async with NodeClient(settings, timeout=5) as client:
                    status = await client.cancel_upgrade(
                        operation.node,
                        job_id,
                        NodeUpgradeCancel(
                            generation=operation.generation,
                            reason="authorization_revoked",
                            unstarted_plan=retained_plan
                            if (
                                operation.phase is UpgradePhase.ACCEPTED
                                and operation.receipt_sequence == 0
                            )
                            else None,
                        ),
                    )
                async with session_scope() as session:
                    row = await get_operation(session, job_id)
                    if row.execution_plan is None:
                        raise UpgradeUnavailable("Revoked operation has no retained plan") from None
                    await reconcile_receipts(
                        session, UpgradeExecutionPlan.model_validate(row.execution_plan), status
                    )
            except (NodeError, UpgradeUnavailable, UpgradeConflict) as error:
                logger.warning(
                    "upgrade revocation reconciliation retains ownership",
                    extra={"job_id": job_id, "error_type": type(error).__name__},
                )
            return False
        except (NodeError, UpgradeUnavailable, UpgradeConflict) as error:
            logger.warning(
                "upgrade reconciliation retains ownership",
                extra={"job_id": job_id, "error_type": type(error).__name__},
            )
            return False


@DBOS.workflow(name="coire.upgrade.studio", max_recovery_attempts=100)
async def studio_upgrade_workflow(job_id: str) -> None:
    while not await reconcile_upgrade_step(job_id):
        await DBOS.sleep_async(1)


@DBOS.workflow(name="coire.upgrade.control_plane", max_recovery_attempts=100)
async def core_upgrade_workflow(job_id: str) -> None:
    while not await reconcile_upgrade_step(job_id):
        await DBOS.sleep_async(1)
