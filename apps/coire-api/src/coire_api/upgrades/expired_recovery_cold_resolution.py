"""Linked after-deadline terminalization, after actual Source and peer closure."""

from datetime import UTC, datetime

from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.db import (
    UpgradeExpiredRecoveryRow,
    UpgradeMaintenanceRow,
    UpgradeOperationRow,
    UpgradeReceiptRow,
)
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.expired_recovery_cold import pending_binding
from coire_api.upgrades.expired_recovery_cold_verification import verify_live_verification
from coire_api.upgrades.expired_recovery_data import capture_data_snapshot, verify_data_preserved
from coire_api.upgrades.expired_recovery_owner import initiating_actor
from coire_api.upgrades.persistence import operation_lock
from coire_api.upgrades.service import smoke_asset
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdClosure,
    UpgradeExpiredRecoveryColdResolution,
    UpgradeExpiredRecoveryColdResolutionRead,
    UpgradeExpiredRecoveryColdResolutionRequest,
    UpgradeExpiredRecoveryColdResolutionState,
    UpgradeExpiredRecoveryDataSnapshot,
    UpgradeExpiredRecoveryIntent,
)
from coire_core.models.upgrades import (
    NodeMaintenanceAck,
    UpgradeEvent,
    UpgradeEvidence,
    UpgradeMaintenance,
    UpgradeOperation,
    UpgradePhase,
)

tracer = trace.get_tracer("coire.api.cold_recovery_resolution")


async def retained_resolution(
    session: AsyncSession, closure: UpgradeExpiredRecoveryColdClosure, node: str
) -> UpgradeExpiredRecoveryColdResolution | None:
    """Terminal replay authenticates the live initiator; it grants no effects."""
    owner = closure.handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
    from coire_api.upgrades.recovery_support_scope import require_original

    require_original(
        owner.intent.request.original_job_id,
        owner.intent.request.original_generation,
        owner.intent.request.expected_source_sha256,
    )
    if node != owner.original_claim.node:
        raise UpgradeForbidden()
    await operation_lock(session)
    row = await session.get(
        UpgradeExpiredRecoveryRow, owner.intent.recovery_id, with_for_update=True
    )
    if row is None:
        raise UpgradeUnavailable("Cold resolution authority is absent")
    await authorize(session, initiating_actor(row))
    if row.resolution_payload is None:
        return None
    if row.phase != "recovered":
        raise UpgradeConflict("Cold resolution has a different retained terminal protocol")
    result = UpgradeExpiredRecoveryColdResolution.model_validate(row.resolution_payload)
    if result.closure != closure or row.phase != "recovered" or row.active_slot is not None:
        raise UpgradeConflict("Late resolution replay changes retained closure")
    await verify_terminal_history(session, result)
    return result


async def verify_terminal_history(
    session: AsyncSession, result: UpgradeExpiredRecoveryColdResolution
) -> None:
    original = await session.get(UpgradeOperationRow, result.operation.job_id, with_for_update=True)
    event = await session.get(
        UpgradeReceiptRow,
        (result.event.job_id, result.event.generation, result.event.sequence),
    )
    if (
        original is None
        or original.active_slot is not None
        or original.phase != UpgradePhase.ROLLED_BACK.value
        or original.payload != result.operation.model_dump(mode="json")
        or event is None
        or event.receipt_sha256 != result.event.sha256()
        or event.payload != result.event.model_dump(mode="json")
    ):
        raise UpgradeConflict("Late resolution retained original event binding changed")


async def read_resolution_state(
    session: AsyncSession,
    recovery_id: str,
    body: UpgradeExpiredRecoveryColdResolutionRead,
    node: str,
) -> UpgradeExpiredRecoveryColdResolutionState:
    """Fixed node metadata only; a pending response never grants effects.

    Terminal history can be inspected after the old deadline or initiator
    revocation. The authenticated node must match the retained original scope.
    Pending state independently requires the exact live handback authority.
    """
    await operation_lock(session)
    row = await session.get(UpgradeExpiredRecoveryRow, recovery_id, with_for_update=True)
    if row is None:
        raise UpgradeUnavailable("Cold resolution state is absent")
    intent = UpgradeExpiredRecoveryIntent.model_validate(row.intent_payload)
    from coire_api.upgrades.recovery_support_scope import require_original

    require_original(
        intent.request.original_job_id,
        intent.request.original_generation,
        intent.request.expected_source_sha256,
    )
    if (
        intent.recovery_id != recovery_id
        or row.intent_sha256 != intent.sha256()
        or row.original_job_id != intent.request.original_job_id
    ):
        raise UpgradeConflict("Cold resolution metadata retained intent binding changed")
    if node != intent.original_plan.operation.node or body.intent_sha256 != intent.sha256():
        raise UpgradeForbidden()
    result = None
    if row.resolution_payload is not None:
        if row.phase != "recovered":
            raise UpgradeConflict("Cold terminal metadata has a different retained protocol")
        result = UpgradeExpiredRecoveryColdResolution.model_validate(row.resolution_payload)
        if (
            row.phase != "recovered"
            or row.active_slot is not None
            or result.closure.sha256() != body.closure_sha256
        ):
            raise UpgradeConflict("Cold terminal metadata retained closure differs")
        await verify_terminal_history(session, result)
    else:
        from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryColdHandbackReceipt

        if row.cold_handback_receipt_payload is None or row.phase != "resolving":
            raise UpgradeUnavailable("Cold resolution pending state has no committed handback")
        handback = UpgradeExpiredRecoveryColdHandbackReceipt.model_validate(
            row.cold_handback_receipt_payload
        )
        proof = handback.helper.handback.verification
        owner = proof.request.smoke.smoke.source.launch.source.projection.owner
        await verify_live_verification(session, recovery_id, proof.sha256(), owner.process, node)
    return UpgradeExpiredRecoveryColdResolutionState(
        intent_sha256=body.intent_sha256,
        closure_sha256=body.closure_sha256,
        resolution=result,
        checked_at=datetime.now(UTC),
    )


async def verify_closure_authority(
    session: AsyncSession, body: UpgradeExpiredRecoveryColdResolutionRequest, node: str
) -> None:
    closure = body.closure
    owner = closure.handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
    proof = closure.handback.helper.handback.verification
    await verify_live_verification(
        session, owner.intent.recovery_id, proof.sha256(), owner.process, node
    )
    row, _ = await pending_binding(session, owner.intent.recovery_id, node)
    if row.phase != "resolving" or row.cold_handback_receipt_payload != closure.handback.model_dump(
        mode="json"
    ):
        raise UpgradeConflict("Cold resolution has no exact committed Source handback")
    if not 0 <= (datetime.now(UTC) - body.observation.observed_at).total_seconds() <= 5:
        raise UpgradeUnavailable("Cold resolution physical closure is stale")
    plan = owner.intent.original_plan
    if await smoke_asset(session, plan.model_id, (node,)) != (
        plan.model_id,
        plan.model_variant_id,
        plan.model_slug,
        plan.model_manifest_sha256,
    ):
        raise UpgradeConflict("Cold resolution registry asset changed")


@tracer.start_as_current_span("coire.api.cold_recovery_resolve")
async def resolve_cold_recovery(
    session: AsyncSession,
    body: UpgradeExpiredRecoveryColdResolutionRequest,
    node: str,
    peer_release: NodeMaintenanceAck | None,
) -> tuple[UpgradeExpiredRecoveryColdResolution, bool]:
    """Caller obtains peer acknowledgement through fixed authenticated transport.

    This transaction retains every prior receipt and the exact execution plan,
    appends a linked late event, and closes both slots and Core fences atomically
    with the audit. No ordinary receipt can produce this terminal phase.
    """
    body = UpgradeExpiredRecoveryColdResolutionRequest.model_validate_json(body.model_dump_json())
    closure = body.closure
    previous = await retained_resolution(session, closure, node)
    if previous is not None:
        return previous, False
    await verify_closure_authority(session, body, node)
    owner = closure.handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
    row, intent = await pending_binding(session, owner.intent.recovery_id, node)
    if row.data_baseline_payload is None:
        raise UpgradeUnavailable("Cold resolution has no retained preservation baseline")
    before = UpgradeExpiredRecoveryDataSnapshot.model_validate(row.data_baseline_payload)
    verify_data_preserved(before, await capture_data_snapshot(session, intent), intent)
    original = await session.get(
        UpgradeOperationRow, intent.request.original_job_id, with_for_update=True
    )
    if original is None:
        raise UpgradeConflict("Cold resolution original operation is absent")
    operation = UpgradeOperation.model_validate(original.payload)
    now = datetime.now(UTC)
    evidence = UpgradeEvidence(
        runtime_sha256=owner.original_native.source.runtime.environment_sha256,
        release_sha256=intent.request.expected_source_sha256,
        peer_runtime_sha256=intent.original_plan.peer_runtime_sha256,
        source_verified=True,
        cleanup_verified=True,
        collective_verified=closure.handback.helper.handback.verification.request.pair is not None,
    )
    event = UpgradeEvent(
        job_id=operation.job_id,
        generation=operation.generation,
        sequence=operation.receipt_sequence + 1,
        effect_id=f"expired-recovery-{intent.recovery_id}",
        phase=UpgradePhase.ROLLED_BACK,
        occurred_at=now,
        outcome="complete",
        observed_sha256=intent.request.expected_source_sha256,
        reason="expired_operation_recovered",
        evidence=evidence.model_dump_json(exclude_none=True),
        restoration="verified",
    )
    resolved_operation = UpgradeOperation.model_validate(
        operation.model_dump(mode="json")
        | {
            "phase": UpgradePhase.ROLLED_BACK,
            "updated_at": now,
            "finished_at": now,
            "cancellation_allowed": False,
            "receipt_sequence": event.sequence,
            "reason": "expired_operation_recovered",
        }
    )
    result = UpgradeExpiredRecoveryColdResolution(
        closure=closure,
        observation=body.observation,
        peer_release=peer_release,
        operation=resolved_operation,
        before_operation=operation,
        event=event,
        resolved_at=now,
    )
    fences = (
        await session.scalars(
            select(UpgradeMaintenanceRow)
            .where(UpgradeMaintenanceRow.job_id == operation.job_id)
            .with_for_update()
        )
    ).all()
    if {fence.target for fence in fences} != set(intent.original_plan.maintenance.targets):
        raise UpgradeConflict("Cold resolution original maintenance scope changed")
    for fence in fences:
        maintenance = UpgradeMaintenance.model_validate(fence.payload)
        if fence.released or not maintenance.matches_plan(intent.original_plan):
            raise UpgradeConflict("Cold resolution original fence is no longer retained")
        fence.released = True
        fence.payload = maintenance.model_copy(
            update={"state": "released", "updated_at": now}
        ).model_dump(mode="json")
    session.add(
        UpgradeReceiptRow(
            job_id=event.job_id,
            generation=event.generation,
            sequence=event.sequence,
            receipt_sha256=event.sha256(),
            payload=event.model_dump(mode="json"),
        )
    )
    original.payload = resolved_operation.model_dump(mode="json")
    original.phase = UpgradePhase.ROLLED_BACK.value
    original.active_slot = None
    row.resolution_payload = result.model_dump(mode="json")
    row.phase = "recovered"
    row.active_slot = None
    await write_principal_audit(
        session,
        principal=initiating_actor(row),
        action="upgrades.expired_recovery.resolve",
        target_type="upgrade_recovery",
        target_id=intent.recovery_id,
        detail={
            "original_job_id": operation.job_id,
            "resolution_sha256": result.sha256(),
            "original_upgrade_succeeded": False,
            "source_sha256": intent.request.expected_source_sha256,
        },
    )
    await session.flush()
    return result, True
