"""Audit durable pending owner transfer; never signal/select/release here."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import UpgradeExpiredRecoveryRow, UpgradeMaintenanceRow, UpgradeOperationRow
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.persistence import get_release, operation_lock
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryIntent,
    UpgradeExpiredRecoveryOwnerTransfer,
)
from coire_core.models.upgrades import UpgradeExecutionPlan, UpgradeOperation, UpgradePhase


async def prepare_transfer(
    session: AsyncSession,
    principal: Principal,
    transfer: UpgradeExpiredRecoveryOwnerTransfer,
) -> tuple[UpgradeExpiredRecoveryOwnerTransfer, bool]:
    """Caller commits audit and immutable transfer before any effect.

    Not effect authority: the separately verified native consumer must reauthorize,
    verify actual processes/bytes/launchd and maintain exclusive ownership/fences.
    Replay is retained intent inspection, never a refreshed transfer lifetime.
    """
    await operation_lock(session)
    owner = await authorize(session, principal)
    transfer = UpgradeExpiredRecoveryOwnerTransfer.model_validate_json(transfer.model_dump_json())
    intent = transfer.intent
    if intent.request.recovery_mode != "preserve":
        raise UpgradeConflict("Cold recovery cannot select a live-preservation transfer")
    row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id, with_for_update=True)
    if (
        row is None
        or row.user_id != owner
        or row.key_id != principal.api_key_id
        or row.credential_version != principal.credential_version
    ):
        raise UpgradeForbidden()
    retained = UpgradeExpiredRecoveryIntent.model_validate(row.intent_payload)
    if retained.sha256() != row.intent_sha256 or retained.sha256() != intent.sha256():
        raise UpgradeConflict("Recovery transfer accepted authority differs")
    if row.transfer_payload is not None:
        previous = UpgradeExpiredRecoveryOwnerTransfer.model_validate(row.transfer_payload)
        if previous.sha256() != transfer.sha256():
            raise UpgradeConflict("Recovery transfer retry changes captured intent")
        return previous, False
    now = datetime.now(UTC)
    if (
        row.active_slot != 1
        or row.phase != "accepted"
        or not 0 <= (now - transfer.issued_at).total_seconds() <= 5
        or transfer.expires_at <= now
    ):
        raise UpgradeUnavailable("Recovery transfer is stale, exhausted or not accepted")
    original = await session.get(
        UpgradeOperationRow, intent.request.original_job_id, with_for_update=True
    )
    if original is None or original.execution_plan is None:
        raise UpgradeConflict("Original recovery authority is absent")
    operation = UpgradeOperation.model_validate(original.payload)
    plan = UpgradeExecutionPlan.model_validate(original.execution_plan)
    if (
        original.active_slot != 1
        or original.phase != "recovery_required"
        or operation.phase is not UpgradePhase.RECOVERY_REQUIRED
        or operation.authority_sha256() != plan.operation.authority_sha256()
        or plan.authority_sha256() != intent.original_plan.authority_sha256()
    ):
        raise UpgradeConflict("Original recovery authority changed before transfer")
    fences = (
        await session.scalars(
            select(UpgradeMaintenanceRow).where(UpgradeMaintenanceRow.job_id == operation.job_id)
        )
    ).all()
    if sorted(f.target for f in fences) != sorted(plan.maintenance.targets) or any(
        f.released or f.generation != operation.generation for f in fences
    ):
        raise UpgradeConflict("Original recovery fence differs before transfer")
    support = await get_release(session, intent.request.support_release_id)
    if support.sha256() != intent.support_release.sha256():
        raise UpgradeConflict("Recovery support catalog differs before transfer")
    row.transfer_payload = transfer.model_dump(mode="json")
    row.phase = "transferring"
    await write_principal_audit(
        session,
        principal=principal,
        action="upgrades.expired_recovery.transfer.prepare",
        target_type="upgrade_recovery",
        target_id=intent.recovery_id,
        detail={"transfer_sha256": transfer.sha256(), "original_job_id": operation.job_id},
    )
    await session.flush()
    return transfer, True
