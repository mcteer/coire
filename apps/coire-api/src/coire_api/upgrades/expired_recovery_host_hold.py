"""Audited terminal hold release; no Source effects or renewed recovery budget."""

import asyncio
from datetime import UTC, datetime

from opentelemetry import metrics, trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import AuditRow, UpgradeExpiredRecoveryRow, UpgradeMaintenanceRow
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.expired_recovery_cold_resolution import verify_terminal_history
from coire_api.upgrades.host_hold import operation_hold
from coire_api.upgrades.persistence import operation_lock
from coire_api.upgrades.recovery_support_scope import require_original
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdResolution,
    UpgradeRecoveryHostHoldReceipt,
    UpgradeRecoveryHostHoldRequest,
)
from coire_core.settings import Settings

ACTION = "upgrades.expired_recovery.host_hold.reconcile"
tracer = trace.get_tracer("coire.api.recovery_host_hold")
reconciliations = metrics.get_meter("coire.api.recovery_host_hold").create_counter(
    "coire_upgrade_recovery_host_hold_reconciliations_total"
)


@tracer.start_as_current_span("coire.api.recovery_host_hold", record_exception=False)
async def reconcile_host_hold(
    session: AsyncSession,
    principal: Principal,
    settings: Settings,
    recovery_id: str,
    request: UpgradeRecoveryHostHoldRequest,
) -> tuple[UpgradeRecoveryHostHoldReceipt, bool]:
    await operation_lock(session)
    actor = await authorize(session, principal)
    row = await session.get(UpgradeExpiredRecoveryRow, recovery_id, with_for_update=True)
    if (
        row is None
        or row.phase != "recovered"
        or row.active_slot is not None
        or row.resolution_payload is None
    ):
        raise UpgradeUnavailable("Host hold reconciliation requires committed recovered authority")
    result = UpgradeExpiredRecoveryColdResolution.model_validate(row.resolution_payload)
    owner = result.closure.handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
    intent = owner.intent
    require_original(
        intent.request.original_job_id,
        intent.request.original_generation,
        intent.request.expected_source_sha256,
    )
    if (
        intent.recovery_id != recovery_id
        or row.intent_sha256 != intent.sha256()
        or result.sha256() != request.resolution_sha256
    ):
        raise UpgradeConflict("Host hold reconciliation changes exact recovered resolution")
    await verify_terminal_history(session, result)
    fences = (
        await session.scalars(
            select(UpgradeMaintenanceRow).where(
                UpgradeMaintenanceRow.job_id == result.operation.job_id
            )
        )
    ).all()
    if {f.target for f in fences} != set(intent.original_plan.maintenance.targets) or any(
        not f.released for f in fences
    ):
        raise UpgradeConflict("Host hold reconciliation retains original Core fencing")
    existing = await session.scalar(
        select(AuditRow).where(AuditRow.action == ACTION, AuditRow.target_id == recovery_id)
    )
    # Release producer verifies exact hold/project/plan and actual shared
    # deployment exclusion, including after a lost effect or audit response.
    await asyncio.to_thread(operation_hold, settings, intent.original_plan, release=True)
    if existing is not None:
        receipt = UpgradeRecoveryHostHoldReceipt.model_validate(existing.detail["receipt"])
        if receipt.resolution_sha256 != request.resolution_sha256:
            raise UpgradeConflict("Host hold replay changes its committed resolution")
        return receipt, False
    receipt = UpgradeRecoveryHostHoldReceipt(
        recovery_id=recovery_id,
        resolution_sha256=request.resolution_sha256,
        original_job_id=result.operation.job_id,
        original_generation=result.operation.generation,
        plan_sha256=intent.original_plan.authority_sha256(),
        reconciled_by=actor,
        reconciled_at=datetime.now(UTC),
    )
    await write_principal_audit(
        session,
        principal=principal,
        action=ACTION,
        target_type="upgrade_recovery",
        target_id=recovery_id,
        detail={"receipt": receipt.model_dump(mode="json"), "execution_budget_unchanged": True},
    )
    await session.flush()
    return receipt, True
