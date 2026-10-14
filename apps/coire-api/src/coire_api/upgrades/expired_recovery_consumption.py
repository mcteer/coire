"""One-time audited transfer consumption; never physical handover completion."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal, PrincipalKind
from coire_api.db import UpgradeExpiredRecoveryRow, UpgradeOperationRow
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.expired_recovery_transfer import prepare_transfer
from coire_api.upgrades.grants import verify_fenced
from coire_api.upgrades.persistence import get_release, operation_lock
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.auth import UserRole
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerTransfer,
    UpgradeExpiredRecoveryTransferConsumption,
)
from coire_core.models.upgrades import (
    NodeAgentProcessIdentity,
    UpgradeExecutionPlan,
    UpgradeOperation,
    UpgradePhase,
)


async def consume_transfer(
    session: AsyncSession,
    principal: Principal,
    transfer: UpgradeExpiredRecoveryOwnerTransfer,
) -> tuple[UpgradeExpiredRecoveryTransferConsumption, bool]:
    """Caller commits receipt/audit before native effects; no I/O under transaction.

    prepare_transfer locks and reauthorizes accepted admin/credential context.
    Replayed receipt is inspection only: native producer must independently
    verify authenticated provenance, actual runtime/process/launchd ownership,
    unexpired scope and local exactly-once effect journal before any mutation.
    """
    transfer, _ = await prepare_transfer(session, principal, transfer)
    intent = transfer.intent
    row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id, with_for_update=True)
    if row is None:
        raise UpgradeUnavailable("Recovery authority disappeared")
    if row.consumption_payload is not None:
        receipt = UpgradeExpiredRecoveryTransferConsumption.model_validate(row.consumption_payload)
        if receipt.transfer.sha256() != transfer.sha256():
            raise UpgradeConflict("Retained transfer consumption differs")
        return receipt, False
    now = datetime.now(UTC)
    if (
        row.active_slot != 1
        or row.phase != "transferring"
        or not transfer.issued_at <= now < transfer.expires_at
    ):
        raise UpgradeUnavailable("Recovery transfer cannot be consumed in this epoch")
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
        or operation.finished_at is not None
        or operation.authority_sha256() != plan.operation.authority_sha256()
        or plan.authority_sha256() != intent.original_plan.authority_sha256()
    ):
        raise UpgradeConflict("Original recovery authority differs at consumption")
    await verify_fenced(session, plan)
    support = await get_release(session, intent.request.support_release_id)
    if support.sha256() != intent.support_release.sha256():
        raise UpgradeConflict("Recovery support differs at consumption")
    receipt = UpgradeExpiredRecoveryTransferConsumption(
        consumption_id=uuid4(), transfer=transfer, consumer=transfer.successor, consumed_at=now
    )
    row.consumption_payload = receipt.model_dump(mode="json")
    row.phase = "transfer_consumed"
    await write_principal_audit(
        session,
        principal=principal,
        action="upgrades.expired_recovery.transfer.consume",
        target_type="upgrade_recovery",
        target_id=intent.recovery_id,
        detail={"consumption_sha256": receipt.sha256(), "original_job_id": operation.job_id},
    )
    await session.flush()
    return receipt, True


async def verify_live_consumption(
    session: AsyncSession,
    recovery_id: str,
    receipt_sha256: str,
    consumer: NodeAgentProcessIdentity,
    node: str,
) -> UpgradeExpiredRecoveryTransferConsumption:
    """Fresh effect gate for an independently authenticated fixed-node caller.

    Not a public input authorization: transport must authenticate the node first.
    Native consumer must still prove current process, bytes and exclusive ownership.
    Never refreshes consumption timestamp or captured transfer/recovery deadlines.
    """
    await operation_lock(session)
    row = await session.get(UpgradeExpiredRecoveryRow, recovery_id, with_for_update=True)
    if row is None or row.consumption_payload is None:
        raise UpgradeUnavailable("Recovery transfer has no retained consumption")
    receipt = UpgradeExpiredRecoveryTransferConsumption.model_validate(row.consumption_payload)
    if (
        receipt.sha256() != receipt_sha256
        or receipt.consumer != consumer
        or receipt.transfer.intent.original_plan.operation.node != node
    ):
        raise UpgradeForbidden()
    actor = Principal(
        kind=PrincipalKind.API_KEY if row.key_id is not None else PrincipalKind.ADMIN,
        user_id=row.user_id,
        role=UserRole.ADMIN,
        scopes=frozenset({"admin"}),
        api_key_id=row.key_id,
        credential_version=row.credential_version,
    )
    await authorize(session, actor)
    retained, _ = await consume_transfer(session, actor, receipt.transfer)
    now = datetime.now(UTC)
    if (
        retained != receipt
        or row.phase != "transfer_consumed"
        or row.active_slot != 1
        or not receipt.consumed_at <= now < receipt.transfer.expires_at
    ):
        raise UpgradeUnavailable("Recovery transfer consumption is no longer live")
    original = await session.get(
        UpgradeOperationRow, receipt.transfer.intent.request.original_job_id
    )
    if original is None or original.execution_plan is None:
        raise UpgradeConflict("Original fenced recovery authority is absent")
    plan = UpgradeExecutionPlan.model_validate(original.execution_plan)
    operation = UpgradeOperation.model_validate(original.payload)
    if (
        original.active_slot != 1
        or original.phase != "recovery_required"
        or operation.phase is not UpgradePhase.RECOVERY_REQUIRED
        or operation.finished_at is not None
        or plan.authority_sha256() != receipt.transfer.intent.original_plan.authority_sha256()
    ):
        raise UpgradeConflict("Original recovery authority differs at effect gate")
    await verify_fenced(session, plan)
    support = await get_release(session, receipt.transfer.intent.request.support_release_id)
    if support.sha256() != receipt.transfer.intent.support_release.sha256():
        raise UpgradeConflict("Recovery support catalog differs at effect gate")
    return receipt
