"""Read the exact immutable continuation link; never infer one from owner absence."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import UpgradeExpiredRecoveryRow, UpgradeRecoverySuccessionRow
from coire_core.errors import UpgradeConflict
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdClaim,
    UpgradeExpiredRecoveryIntent,
)
from coire_core.models.upgrade_recovery_continuation import (
    UpgradeColdContinuationGrant,
    UpgradeColdRecoverySupersession,
)


async def retained_binding(
    session: AsyncSession, intent: UpgradeExpiredRecoveryIntent
) -> UpgradeColdContinuationGrant | None:
    rows = (
        await session.scalars(
            select(UpgradeRecoverySuccessionRow).where(
                UpgradeRecoverySuccessionRow.recovery_id == intent.recovery_id
            )
        )
    ).all()
    if not rows:
        return None
    if len(rows) != 1 or rows[0].grant_payload.get("protocol") != "cold-source-continuation-v1":
        raise UpgradeConflict("Cold recovery has another or ambiguous succession protocol")
    row = rows[0]
    grant = UpgradeColdContinuationGrant.model_validate(row.grant_payload)
    if (
        grant.intent != intent
        or grant.grant_id != row.id
        or grant.sha256() != row.grant_sha256
        or grant.binding.previous_owner_sha256 != row.predecessor_sha256
    ):
        raise UpgradeConflict("Cold continuation immutable grant differs")
    predecessor = await session.get(UpgradeExpiredRecoveryRow, grant.binding.previous_recovery_id)
    if (
        predecessor is None
        or predecessor.phase != "superseded"
        or predecessor.active_slot is not None
        or predecessor.cold_claim_payload is None
        or predecessor.resolution_payload is None
    ):
        raise UpgradeConflict("Cold continuation predecessor is not exactly superseded")
    owner = UpgradeExpiredRecoveryColdClaim.model_validate(predecessor.cold_claim_payload)
    custody = predecessor
    if grant.unstarted_startup is not None:
        retained_custody = await session.get(
            UpgradeExpiredRecoveryRow, grant.unstarted_startup.intent.recovery_id
        )
        if retained_custody is None:
            raise UpgradeConflict("Cold unstarted custodian is absent")
        custody = retained_custody
        if (
            custody is None
            or custody.phase != "superseded"
            or custody.active_slot is not None
            or custody.cold_claim_payload is not None
            or custody.resolution_payload is None
        ):
            raise UpgradeConflict("Cold unstarted custodian is not exactly superseded")
        if custody.intent_payload != grant.unstarted_startup.intent.model_dump(mode="json"):
            raise UpgradeConflict("Cold unstarted custody intent differs")
    receipt = UpgradeColdRecoverySupersession.model_validate(custody.resolution_payload)
    if (
        owner.sha256() != grant.binding.previous_owner_sha256
        or owner.intent.sha256() != grant.binding.previous_intent_sha256
        or predecessor.intent_sha256 != owner.intent.sha256()
        or predecessor.original_job_id != intent.request.original_job_id
        or owner.original_claim != grant.binding.original_claim
        or owner.original_native != grant.binding.original_native
        or owner.kernel_boot_identity != grant.binding.previous_kernel_boot_identity
        or owner.retired_supervisor is None
        or owner.retired_supervisor.sha256() != grant.binding.retired_supervisor_sha256
        or receipt.grant_sha256 != grant.sha256()
        or receipt.next_recovery_id != intent.recovery_id
        or receipt.next_intent_sha256 != intent.sha256()
        or receipt.previous_recovery_id != custody.id
        or receipt.previous_intent_sha256 != custody.intent_sha256
        or receipt.previous_owner_sha256 != owner.sha256()
        or receipt.previous_deadline_at
        != UpgradeExpiredRecoveryIntent.model_validate(custody.intent_payload).deadline_at
        or receipt.unstarted_startup != grant.unstarted_startup
    ):
        raise UpgradeConflict("Cold continuation changes predecessor history or linkage")
    return grant
