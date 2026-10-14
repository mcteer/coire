"""Audit acknowledgement of canonical recovery, without new execution or qualification."""

from datetime import UTC, datetime

from opentelemetry import metrics, trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import (
    AuditRow,
    UpgradeExpiredRecoveryRow,
    UpgradeMaintenanceRow,
    UpgradeOperationRow,
)
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.expired_recovery_cold_resolution import verify_terminal_history
from coire_api.upgrades.persistence import operation_lock
from coire_api.upgrades.recovery_support_scope import require_original
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrade_native_terminal import (
    UpgradeNativeTerminalGrant,
    UpgradeNativeTerminalRequest,
    UpgradeNativeTerminalState,
)
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryColdResolution
from coire_core.models.upgrades import TERMINAL_UPGRADE_PHASES, UpgradeOperation

ACTION = "upgrades.expired_recovery.native_terminal.request"
requests = metrics.get_meter("coire.api.native_terminal").create_counter(
    "coire_upgrade_native_terminal_requests_total"
)
tracer = trace.get_tracer("coire.api.native_terminal")


async def resolved(session: AsyncSession, recovery_id: str) -> UpgradeExpiredRecoveryColdResolution:
    row = await session.get(UpgradeExpiredRecoveryRow, recovery_id, with_for_update=True)
    if (
        row is None
        or row.phase != "recovered"
        or row.active_slot is not None
        or row.resolution_payload is None
    ):
        raise UpgradeUnavailable("Native acknowledgement requires committed recovered authority")
    result = UpgradeExpiredRecoveryColdResolution.model_validate(row.resolution_payload)
    owner = result.closure.handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
    request = owner.intent.request
    require_original(
        request.original_job_id, request.original_generation, request.expected_source_sha256
    )
    await verify_terminal_history(session, result)
    fences = (
        await session.scalars(
            select(UpgradeMaintenanceRow).where(
                UpgradeMaintenanceRow.target.in_(owner.intent.original_plan.maintenance.targets)
            )
        )
    ).all()
    if {f.target for f in fences} != set(owner.intent.original_plan.maintenance.targets) or any(
        not f.released or f.generation < request.original_generation for f in fences
    ):
        raise UpgradeConflict("Native acknowledgement retains original Core fences")
    for fence in fences:
        if (fence.job_id, fence.generation) == (
            request.original_job_id,
            request.original_generation,
        ):
            continue
        terminal = await session.get(UpgradeOperationRow, fence.job_id)
        if terminal is None or terminal.active_slot is not None:
            raise UpgradeConflict("Native acknowledgement has unknown superseding Core fencing")
        terminal_operation = UpgradeOperation.model_validate(terminal.payload)
        if (
            terminal_operation.phase not in TERMINAL_UPGRADE_PHASES
            or terminal_operation.generation != fence.generation
        ):
            raise UpgradeConflict("Native acknowledgement superseding Core fence is not terminal")
    if (
        await session.scalar(
            select(UpgradeOperationRow.id).where(UpgradeOperationRow.active_slot == 1).limit(1)
        )
        is not None
        or await session.scalar(
            select(UpgradeExpiredRecoveryRow.id)
            .where(UpgradeExpiredRecoveryRow.active_slot == 1)
            .limit(1)
        )
        is not None
    ):
        raise UpgradeConflict("Native acknowledgement conflicts with active operation authority")
    return result


@tracer.start_as_current_span("coire.api.native_terminal", record_exception=False)
async def prepare_native_terminal(
    session: AsyncSession,
    principal: Principal,
    recovery_id: str,
    request: UpgradeNativeTerminalRequest,
) -> UpgradeNativeTerminalGrant:
    await operation_lock(session)
    actor = await authorize(session, principal)
    result = await resolved(session, recovery_id)
    if result.sha256() != request.resolution_sha256:
        raise UpgradeConflict("Native acknowledgement resolution differs")
    audit = await session.scalar(
        select(AuditRow).where(AuditRow.action == ACTION, AuditRow.target_id == recovery_id)
    )
    if audit is not None:
        if audit.detail["resolution_sha256"] != request.resolution_sha256:
            raise UpgradeConflict("Native acknowledgement replay differs")
        return UpgradeNativeTerminalGrant.model_validate(
            {
                "resolution": result,
                "approved_by": audit.detail["approved_by"],
                "approved_at": audit.detail["approved_at"],
            }
        )
    grant = UpgradeNativeTerminalGrant(
        resolution=result, approved_by=actor, approved_at=datetime.now(UTC)
    )
    await write_principal_audit(
        session,
        principal=principal,
        action=ACTION,
        target_type="upgrade_recovery",
        target_id=recovery_id,
        detail={
            "resolution_sha256": request.resolution_sha256,
            "approved_by": str(actor),
            "approved_at": grant.approved_at.isoformat(),
            "new_execution": False,
            "new_qualification": False,
        },
    )
    await session.flush()
    return grant


async def native_terminal_state(
    session: AsyncSession, recovery_id: str, node: str
) -> UpgradeNativeTerminalState:
    await operation_lock(session)
    result = await resolved(session, recovery_id)
    if result.operation.node != node:
        raise UpgradeForbidden("Native acknowledgement targets another Studio")
    audit = await session.scalar(
        select(AuditRow).where(AuditRow.action == ACTION, AuditRow.target_id == recovery_id)
    )
    if audit is None or audit.detail["resolution_sha256"] != result.sha256():
        raise UpgradeForbidden("Native acknowledgement has no exact audited admin request")
    grant = UpgradeNativeTerminalGrant.model_validate(
        {
            "resolution": result,
            "approved_by": audit.detail["approved_by"],
            "approved_at": audit.detail["approved_at"],
        }
    )
    return UpgradeNativeTerminalState(grant=grant, observed_at=datetime.now(UTC))
