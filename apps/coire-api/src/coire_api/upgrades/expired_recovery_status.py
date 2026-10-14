"""Live-admin durable recovery inspection, retaining expired failure history."""

from datetime import UTC, datetime

from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import UpgradeExpiredRecoveryRow
from coire_api.upgrades.authorization import authorize
from coire_core.errors import UpgradeConflict, UpgradeNotFound
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryCancellation,
    UpgradeExpiredRecoveryColdClaim,
    UpgradeExpiredRecoveryIntent,
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoveryStatus,
)

tracer = trace.get_tracer("coire.api.recovery_status")


@tracer.start_as_current_span("coire.api.recovery_status")
async def read_recovery_status(
    session: AsyncSession, principal: Principal, recovery_id: str
) -> UpgradeExpiredRecoveryStatus:
    await authorize(session, principal)
    row = await session.get(UpgradeExpiredRecoveryRow, recovery_id)
    if row is None:
        raise UpgradeNotFound()
    intent = UpgradeExpiredRecoveryIntent.model_validate(row.intent_payload)
    from coire_api.upgrades.recovery_support_scope import require_original

    require_original(
        intent.request.original_job_id,
        intent.request.original_generation,
        intent.request.expected_source_sha256,
    )
    if intent.recovery_id != recovery_id or intent.sha256() != row.intent_sha256:
        raise UpgradeConflict("Retained recovery inspection authority differs")
    owner = (
        UpgradeExpiredRecoveryColdClaim.model_validate(row.cold_claim_payload)
        if row.cold_claim_payload is not None
        else UpgradeExpiredRecoveryOwnerClaim.model_validate(row.owner_claim_payload)
        if row.owner_claim_payload is not None
        else None
    )
    return UpgradeExpiredRecoveryStatus.model_validate(
        {
            "intent": intent,
            "phase": row.phase,
            "active": row.active_slot == 1,
            "cancellation": UpgradeExpiredRecoveryCancellation.model_validate(
                row.cancellation_payload
            )
            if row.cancellation_payload is not None
            else None,
            "owner_sha256": owner.sha256() if owner is not None else None,
            "observed_at": datetime.now(UTC),
        }
    )
