"""Transactional administrative acceptance of separately captured recovery intent.

No transport/effect producer is exposed here: callers must verify authenticated
native observations and capture the intent before this transaction. Acceptance
retains the original operation/fences and never authorizes Source replacement.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import (
    UpgradeExpiredRecoveryRow,
    UpgradeHandoverRow,
    UpgradeMaintenanceRow,
    UpgradeOperationRow,
)
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.persistence import get_release, operation_lock
from coire_core.errors import UpgradeConflict, UpgradeNotFound, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryDataSnapshot,
    UpgradeExpiredRecoveryIntent,
    UpgradeExpiredRecoveryNativeDataSnapshot,
)
from coire_core.models.upgrades import UpgradeExecutionPlan, UpgradeOperation, UpgradePhase


async def accept_expired_recovery(
    session: AsyncSession,
    principal: Principal,
    intent: UpgradeExpiredRecoveryIntent,
    *,
    data_baseline: UpgradeExpiredRecoveryDataSnapshot | None = None,
    native_baseline: UpgradeExpiredRecoveryNativeDataSnapshot | None = None,
) -> tuple[UpgradeExpiredRecoveryIntent, bool]:
    """Caller owns commit: intent and audit succeed or roll back together.

    An idempotent retry returns the first captured epoch even if the caller has
    unnecessarily recaptured another one. Execution must separately reauthorize.
    """
    await operation_lock(session)
    owner = await authorize(session, principal)
    intent = UpgradeExpiredRecoveryIntent.model_validate_json(intent.model_dump_json())
    if intent.accepted_by != owner:
        raise UpgradeConflict("Expired recovery administrative actor differs")
    request = intent.request
    from coire_api.upgrades.recovery_support_scope import require_original

    require_original(
        request.original_job_id, request.original_generation, request.expected_source_sha256
    )
    existing = await session.scalar(
        select(UpgradeExpiredRecoveryRow).where(
            UpgradeExpiredRecoveryRow.user_id == owner,
            UpgradeExpiredRecoveryRow.idempotency_key == request.idempotency_key,
        )
    )
    if existing is not None:
        retained = UpgradeExpiredRecoveryIntent.model_validate(existing.intent_payload)
        if (
            existing.request_sha256 != request.sha256()
            or existing.intent_sha256 != retained.sha256()
        ):
            raise UpgradeConflict("Expired recovery idempotency or retained authority differs")
        return retained, False
    now = datetime.now(UTC)
    if not 0 <= (now - intent.accepted_at).total_seconds() <= 5 or intent.deadline_at <= now:
        raise UpgradeUnavailable("Expired recovery capture is stale, future or exhausted")
    if await session.scalar(
        select(UpgradeExpiredRecoveryRow.id).where(UpgradeExpiredRecoveryRow.active_slot == 1)
    ):
        raise UpgradeConflict("Another recovery authority owns the operation")
    if await session.scalar(
        select(UpgradeHandoverRow.id).where(UpgradeHandoverRow.active_slot == 1)
    ):
        raise UpgradeConflict("Executor handover still owns the operation")
    original = await session.get(UpgradeOperationRow, request.original_job_id, with_for_update=True)
    if original is None or original.execution_plan is None:
        raise UpgradeNotFound()
    operation = UpgradeOperation.model_validate(original.payload)
    plan = UpgradeExecutionPlan.model_validate(original.execution_plan)
    if (
        original.active_slot != 1
        or original.phase != UpgradePhase.RECOVERY_REQUIRED.value
        or operation.phase is not UpgradePhase.RECOVERY_REQUIRED
        or operation.finished_at is not None
        or plan.authority_sha256() != intent.original_plan.authority_sha256()
        or operation.authority_sha256() != plan.operation.authority_sha256()
    ):
        raise UpgradeConflict("Original recovery authority changed before acceptance")
    fences = (
        await session.scalars(
            select(UpgradeMaintenanceRow).where(UpgradeMaintenanceRow.job_id == original.id)
        )
    ).all()
    if sorted(f.target for f in fences) != sorted(plan.maintenance.targets) or any(
        f.released or f.generation != operation.generation for f in fences
    ):
        raise UpgradeConflict("Original maintenance fence is incomplete or differs")
    support = await get_release(session, request.support_release_id)
    if support.sha256() != intent.support_release.sha256():
        raise UpgradeConflict("Recovery support differs from immutable catalog")
    if data_baseline is not None:
        from coire_api.upgrades.expired_recovery_data import verify_data_preserved

        verify_data_preserved(data_baseline, data_baseline, intent)
    if native_baseline is not None and (
        (
            native_baseline.original_job_id,
            native_baseline.original_generation,
            native_baseline.source_sha256,
            native_baseline.node,
        )
        != (
            request.original_job_id,
            request.original_generation,
            request.expected_source_sha256,
            intent.original_plan.operation.node,
        )
        or not 0 <= (intent.accepted_at - native_baseline.observed_at).total_seconds() <= 5
    ):
        raise UpgradeConflict(
            "Recovery native data baseline differs from observed original authority"
        )
    session.add(
        UpgradeExpiredRecoveryRow(
            id=intent.recovery_id,
            original_job_id=original.id,
            user_id=owner,
            key_id=principal.api_key_id,
            credential_version=principal.credential_version,
            idempotency_key=request.idempotency_key,
            request_sha256=request.sha256(),
            intent_sha256=intent.sha256(),
            intent_payload=intent.model_dump(mode="json"),
            native_baseline_payload=native_baseline.model_dump(mode="json")
            if native_baseline is not None
            else None,
            data_baseline_payload=data_baseline.model_dump(mode="json")
            if data_baseline is not None
            else None,
            active_slot=1,
            phase="accepted",
        )
    )
    await write_principal_audit(
        session,
        principal=principal,
        action="upgrades.expired_recovery.accept",
        target_type="upgrade_recovery",
        target_id=intent.recovery_id,
        detail={
            "original_job_id": original.id,
            "original_generation": operation.generation,
            "intent_sha256": intent.sha256(),
        },
    )
    await session.flush()
    return intent, True
