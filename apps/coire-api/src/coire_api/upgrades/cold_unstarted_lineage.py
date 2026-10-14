"""Follow immutable custody links without reviving an expired physical owner."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import UpgradeExpiredRecoveryRow, UpgradeRecoverySuccessionRow
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryCancellation,
    UpgradeExpiredRecoveryColdClaim,
    UpgradeExpiredRecoveryIntent,
)
from coire_core.models.upgrade_recovery_continuation import (
    UpgradeColdContinuationGrant,
    UpgradeColdRecoverySupersession,
)


async def unstarted_descendant(
    session: AsyncSession,
    original: UpgradeExpiredRecoveryRow,
    owner: UpgradeExpiredRecoveryColdClaim,
) -> tuple[UpgradeExpiredRecoveryRow, UpgradeExpiredRecoveryIntent] | None:
    if original.phase != "superseded":
        return None
    row = original
    seen = {row.id}
    for _ in range(16):
        if row.resolution_payload is None:
            raise UpgradeConflict("Cold custody link has no immutable supersession")
        link = UpgradeColdRecoverySupersession.model_validate(row.resolution_payload)
        if (
            link.previous_recovery_id != row.id
            or link.previous_intent_sha256 != row.intent_sha256
            or link.previous_owner_sha256 != owner.sha256()
        ):
            raise UpgradeConflict("Cold custody link changes its predecessor")
        if link.next_recovery_id in seen:
            raise UpgradeConflict("Cold custody links repeat")
        seen.add(link.next_recovery_id)
        next_row = await session.get(
            UpgradeExpiredRecoveryRow, link.next_recovery_id, with_for_update=True
        )
        stored = await session.scalar(
            select(UpgradeRecoverySuccessionRow).where(
                UpgradeRecoverySuccessionRow.recovery_id == link.next_recovery_id
            )
        )
        if next_row is None or stored is None:
            raise UpgradeUnavailable("Cold custody successor or grant is absent")
        grant = UpgradeColdContinuationGrant.model_validate(stored.grant_payload)
        intent = UpgradeExpiredRecoveryIntent.model_validate(next_row.intent_payload)
        if (
            grant.intent != intent
            or grant.sha256() != stored.grant_sha256
            or link.grant_sha256 != grant.sha256()
            or link.next_intent_sha256 != intent.sha256()
            or next_row.intent_sha256 != intent.sha256()
            or grant.binding.previous_owner_sha256 != owner.sha256()
            or grant.binding.previous_recovery_id != owner.intent.recovery_id
            or stored.predecessor_sha256 != owner.sha256()
            or intent.original_plan.authority_sha256()
            != owner.intent.original_plan.authority_sha256()
            or any(
                value is not None
                for value in (
                    next_row.transfer_payload,
                    next_row.consumption_payload,
                    next_row.owner_claim_payload,
                    next_row.cold_claim_payload,
                    next_row.cold_smoke_payload,
                    next_row.cold_verification_payload,
                    next_row.cold_handback_intent_payload,
                    next_row.cold_handback_receipt_payload,
                )
            )
        ):
            raise UpgradeConflict("Cold unstarted custody has issued effects or another Source")
        if next_row.phase == "superseded" and next_row.active_slot is None:
            row = next_row
            continue
        cancellation = (
            UpgradeExpiredRecoveryCancellation.model_validate(next_row.cancellation_payload)
            if next_row.cancellation_payload is not None
            else None
        )
        if (
            next_row.phase != "recovery_required"
            or next_row.active_slot != 1
            or next_row.resolution_payload is not None
            or cancellation is None
            or cancellation.recovery_id != intent.recovery_id
            or cancellation.intent_sha256 != intent.sha256()
            or datetime.now(UTC) < intent.deadline_at
        ):
            raise UpgradeUnavailable("Cold unstarted custody is not expired, cancelled and fenced")
        return next_row, intent
    raise UpgradeUnavailable("Cold custody traversal exceeds its bound")
