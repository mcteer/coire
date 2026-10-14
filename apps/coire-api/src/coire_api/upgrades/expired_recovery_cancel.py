"""Audited revocation of separate recovery effects, retaining all obligations."""

import logging
from datetime import UTC, datetime

from opentelemetry import metrics, trace
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import UpgradeExpiredRecoveryRow
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.persistence import operation_lock
from coire_core.errors import UpgradeConflict, UpgradeNotFound
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryCancellation,
    UpgradeExpiredRecoveryCancelRequest,
    UpgradeExpiredRecoveryIntent,
)

tracer = trace.get_tracer("coire.api.recovery_cancel")
logger = logging.getLogger(__name__)
cancellations = metrics.get_meter("coire.api.recovery_cancel").create_counter(
    "coire_upgrade_recovery_cancellations_total"
)


@tracer.start_as_current_span(
    "coire.api.recovery_cancel", record_exception=False, set_status_on_exception=False
)
async def cancel_recovery(
    session: AsyncSession,
    principal: Principal,
    recovery_id: str,
    request: UpgradeExpiredRecoveryCancelRequest,
) -> tuple[UpgradeExpiredRecoveryCancellation, bool]:
    """Caller commits receipt and audit together; replay never recaptures time.

    Any live administrator may stop authority, including after its initiating
    credential is revoked or its deadline expires. No new native effects, cleanup
    assertions, original state mutations or fence/active-slot release follow.
    The same operation lock serializes cancellation against grants and claims.
    """
    await operation_lock(session)
    actor = await authorize(session, principal)
    row = await session.get(UpgradeExpiredRecoveryRow, recovery_id, with_for_update=True)
    if row is None:
        raise UpgradeNotFound()
    intent = UpgradeExpiredRecoveryIntent.model_validate(row.intent_payload)
    from coire_api.upgrades.recovery_support_scope import require_original

    require_original(
        intent.request.original_job_id,
        intent.request.original_generation,
        intent.request.expected_source_sha256,
    )
    if (
        intent.recovery_id != recovery_id
        or intent.sha256() != row.intent_sha256
        or request.intent_sha256 != row.intent_sha256
        or row.original_job_id != intent.request.original_job_id
    ):
        raise UpgradeConflict("Recovery cancellation authority differs")
    if row.cancellation_payload is not None:
        retained = UpgradeExpiredRecoveryCancellation.model_validate(row.cancellation_payload)
        if (
            retained.recovery_id != recovery_id
            or retained.intent_sha256 != intent.sha256()
            or retained.original_job_id != intent.request.original_job_id
            or retained.original_generation != intent.original_plan.operation.generation
            or row.active_slot != 1
            or row.phase != "recovery_required"
        ):
            raise UpgradeConflict("Retained recovery cancellation differs")
        return retained, False
    if row.active_slot != 1 or row.resolution_payload is not None:
        raise UpgradeConflict("Resolved recovery authority cannot be cancelled")
    cancellation = UpgradeExpiredRecoveryCancellation(
        recovery_id=recovery_id,
        intent_sha256=intent.sha256(),
        original_job_id=intent.request.original_job_id,
        original_generation=intent.original_plan.operation.generation,
        cancelled_by=actor,
        cancelled_at=datetime.now(UTC),
    )
    row.cancellation_payload = cancellation.model_dump(mode="json")
    row.phase = "recovery_required"
    await write_principal_audit(
        session,
        principal=principal,
        action="upgrades.expired_recovery.cancel",
        target_type="upgrade_recovery",
        target_id=recovery_id,
        detail={
            "original_job_id": cancellation.original_job_id,
            "original_generation": cancellation.original_generation,
            "intent_sha256": cancellation.intent_sha256,
            "maintenance_retained": True,
        },
    )
    await session.flush()
    return cancellation, True


def record_cancellation(result: UpgradeExpiredRecoveryCancellation, created: bool) -> None:
    """Route calls only after successful commit, so failed audit is not counted."""
    if created:
        cancellations.add(1, {"outcome": "cancelled"})
        logger.warning(
            "native recovery cancelled with obligations retained",
            extra={
                "job_id": result.original_job_id,
                "recovery_id": result.recovery_id,
                "generation": result.original_generation,
                "user_id": str(result.cancelled_by),
            },
        )
