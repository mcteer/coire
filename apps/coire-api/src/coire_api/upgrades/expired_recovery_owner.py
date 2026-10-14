"""Audited fixed-node owner acceptance and live separately budgeted owner gate."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    UpgradeExpiredRecoveryRow,
    UpgradeOperationRow,
    UpgradeRecoverySuccessionRow,
)
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.expired_recovery_consumption import verify_live_consumption
from coire_api.upgrades.grants import verify_fenced
from coire_api.upgrades.persistence import get_release, operation_lock
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.auth import UserRole
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryIntent,
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoveryOwnerState,
    UpgradeExpiredRecoveryTransferConsumption,
)
from coire_core.models.upgrades import (
    NodeAgentProcessIdentity,
    UpgradeExecutionPlan,
    UpgradeOperation,
    UpgradePhase,
)


def initiating_actor(row: UpgradeExpiredRecoveryRow) -> Principal:
    return Principal(
        kind=PrincipalKind.API_KEY if row.key_id is not None else PrincipalKind.ADMIN,
        user_id=row.user_id,
        role=UserRole.ADMIN,
        scopes=frozenset({"admin"}),
        api_key_id=row.key_id,
        credential_version=row.credential_version,
    )


async def verify_intent_binding(
    session: AsyncSession,
    row: UpgradeExpiredRecoveryRow,
    intent: UpgradeExpiredRecoveryIntent,
    node: str,
) -> Principal:
    from coire_api.upgrades.recovery_support_scope import require_original

    require_original(
        intent.request.original_job_id,
        intent.request.original_generation,
        intent.request.expected_source_sha256,
    )
    if intent.original_plan.operation.node != node:
        raise UpgradeForbidden()
    actor = initiating_actor(row)
    await authorize(session, actor)
    if (
        row.intent_sha256 != intent.sha256()
        or row.original_job_id != intent.request.original_job_id
    ):
        raise UpgradeConflict("Recovery owner retained authority differs")
    original = await session.get(
        UpgradeOperationRow, intent.request.original_job_id, with_for_update=True
    )
    if original is None or original.execution_plan is None:
        raise UpgradeConflict("Original recovery owner binding is absent")
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
        raise UpgradeConflict("Original recovery owner binding changed")
    await verify_fenced(session, plan)
    if (
        await get_release(session, intent.request.support_release_id)
    ).sha256() != intent.support_release.sha256():
        raise UpgradeConflict("Recovery owner support catalog changed")
    return actor


async def verify_binding(
    session: AsyncSession,
    row: UpgradeExpiredRecoveryRow,
    claim: UpgradeExpiredRecoveryOwnerClaim,
    node: str,
) -> Principal:
    actor = await verify_intent_binding(session, row, claim.consumption.transfer.intent, node)
    if row.consumption_payload != claim.consumption.model_dump(mode="json"):
        raise UpgradeConflict("Recovery owner retained consumption differs")
    return actor


async def read_live_support_context(
    session: AsyncSession,
    recovery_id: str,
    node: str,
) -> UpgradeExpiredRecoveryIntent:
    """Trusted passive support startup context; not transfer/effect authority.

    Fixed authenticated node transport required. Conservatively require fully
    acknowledged original maintenance even for helper startup; no paths created,
    budget recaptured, model acquisition or original state mutation here.
    """
    await operation_lock(session)
    row = await session.get(UpgradeExpiredRecoveryRow, recovery_id, with_for_update=True)
    if row is None:
        raise UpgradeUnavailable("Recovery support context is absent")
    intent = UpgradeExpiredRecoveryIntent.model_validate(row.intent_payload)
    await verify_intent_binding(session, row, intent, node)
    if (
        row.active_slot != 1
        or row.phase
        not in {
            "accepted",
            "transferring",
            "transfer_consumed",
            "recovering",
            "verifying",
            "cold_recovering",
        }
        or not intent.accepted_at <= datetime.now(UTC) < intent.deadline_at
    ):
        raise UpgradeUnavailable("Recovery support context is no longer live")
    return intent


async def accept_owner_claim(
    session: AsyncSession,
    claim: UpgradeExpiredRecoveryOwnerClaim,
    node: str,
) -> tuple[UpgradeExpiredRecoveryOwnerClaim, bool]:
    """Transport authenticates fixed node; local producer holds physical lease.

    DTO and persistence alone are not actual physical ownership. Replay is
    inspection only; native effects require independent fresh owner gate.
    """
    claim = UpgradeExpiredRecoveryOwnerClaim.model_validate_json(claim.model_dump_json())
    receipt = claim.consumption
    recovery_id = receipt.transfer.intent.recovery_id
    await operation_lock(session)
    row = await session.get(UpgradeExpiredRecoveryRow, recovery_id, with_for_update=True)
    if row is None:
        raise UpgradeUnavailable("Recovery owner authority is absent")
    actor = await verify_binding(session, row, claim, node)
    if row.owner_claim_payload is not None:
        previous = UpgradeExpiredRecoveryOwnerClaim.model_validate(row.owner_claim_payload)
        if previous.sha256() != claim.sha256():
            raise UpgradeConflict("Retained recovery owner differs")
        return previous, False
    await verify_live_consumption(session, recovery_id, receipt.sha256(), claim.process, node)
    now = datetime.now(UTC)
    if not 0 <= (now - claim.claimed_at).total_seconds() <= 5 or now >= receipt.transfer.expires_at:
        raise UpgradeUnavailable("Recovery owner claim is stale")
    row.owner_claim_payload = claim.model_dump(mode="json")
    row.phase = "recovering"
    await write_principal_audit(
        session,
        principal=actor,
        action="upgrades.expired_recovery.owner.accept",
        target_type="upgrade_recovery",
        target_id=recovery_id,
        detail={"owner_sha256": claim.sha256(), "original_job_id": row.original_job_id},
    )
    await session.flush()
    return claim, True


async def probe_owner_acceptance(
    session: AsyncSession,
    recovery_id: str,
    owner_sha256: str,
    process: NodeAgentProcessIdentity,
    node: str,
) -> UpgradeExpiredRecoveryOwnerState:
    """Resolve lost Core commit response without unsafe error-based fallback.

    Independently authenticated fixed node transport required. Unaccepted is
    returned only after positive live consumed initial authority, not absence
    alone; it grants no effect/replay renewal or different actor succession.
    """
    await operation_lock(session)
    row = await session.get(UpgradeExpiredRecoveryRow, recovery_id, with_for_update=True)
    if row is None or row.consumption_payload is None:
        raise UpgradeUnavailable("Recovery owner state has no consumed authority")
    if row.owner_claim_payload is not None:
        owner = await verify_live_owner(session, recovery_id, owner_sha256, process, node)
    else:
        receipt = UpgradeExpiredRecoveryTransferConsumption.model_validate(row.consumption_payload)
        await verify_live_consumption(session, recovery_id, receipt.sha256(), process, node)
        owner = None
    return UpgradeExpiredRecoveryOwnerState(
        owner_sha256=owner_sha256, owner=owner, checked_at=datetime.now(UTC)
    )


async def verify_live_owner(
    session: AsyncSession,
    recovery_id: str,
    owner_sha256: str,
    process: NodeAgentProcessIdentity,
    node: str,
) -> UpgradeExpiredRecoveryOwnerClaim:
    """Reauthorize every boundary against once-captured separate deadline.

    Never extends the transfer window or original expired deadlines. Caller must
    separately prove it still holds the local kernel/physical owner lease.
    """
    await operation_lock(session)
    row = await session.get(UpgradeExpiredRecoveryRow, recovery_id, with_for_update=True)
    if row is None or row.owner_claim_payload is None:
        raise UpgradeUnavailable("Recovery owner has no retained claim")
    claim = UpgradeExpiredRecoveryOwnerClaim.model_validate(row.owner_claim_payload)
    if claim.sha256() != owner_sha256 or claim.process != process:
        raise UpgradeForbidden()
    await verify_binding(session, row, claim, node)
    if (
        await session.scalar(
            select(UpgradeRecoverySuccessionRow.id).where(
                UpgradeRecoverySuccessionRow.recovery_id == recovery_id,
                UpgradeRecoverySuccessionRow.predecessor_sha256 == claim.sha256(),
            )
        )
        is not None
    ):
        raise UpgradeUnavailable("Recovery predecessor effect authority is superseded")
    intent = claim.consumption.transfer.intent
    now = datetime.now(UTC)
    if (
        row.active_slot != 1
        or row.phase not in {"recovering", "verifying"}
        or not claim.claimed_at <= now < intent.deadline_at
    ):
        raise UpgradeUnavailable("Recovery owner is no longer live")
    return claim
