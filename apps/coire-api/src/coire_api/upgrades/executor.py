"""Restricted executor access to one accepted Core operation and retained recovery proof."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import UpgradeExecutorRow, UpgradeOperationRow
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.grants import initiating_actor
from coire_api.upgrades.persistence import get_operation, operation_lock
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.audit import AuditOutcome
from coire_core.models.upgrades import (
    UpgradeClaimIdle,
    UpgradeClaimRequest,
    UpgradeExecutionPlan,
    UpgradeKind,
    UpgradeOperation,
)
from coire_core.settings import Settings


async def scoped_operation(
    session: AsyncSession,
    job_id: str,
    principal: Principal,
    settings: Settings,
    *,
    lock: bool = False,
) -> tuple[UpgradeOperationRow, UpgradeExecutionPlan]:
    if not settings.upgrades_enabled:
        raise UpgradeUnavailable("Managed upgrades are disabled")
    if lock:
        await operation_lock(session)
    row = await get_operation(session, job_id, lock=lock)
    if row.execution_plan is None:
        raise UpgradeConflict("Executor operation has no accepted plan")
    plan = UpgradeExecutionPlan.model_validate(row.execution_plan)
    if (
        plan.operation.kind is not UpgradeKind.CONTROL_PLANE
        or plan.source.manifest.project != settings.upgrade_project
        or row.executor_identity != principal.subject
        or principal.subject != settings.upgrade_executor_id
    ):
        raise UpgradeForbidden("Executor request is outside its accepted project operation")
    return row, plan


async def claim(
    session: AsyncSession,
    principal: Principal,
    request: UpgradeClaimRequest,
    settings: Settings,
) -> UpgradeExecutionPlan | UpgradeClaimIdle:
    if not settings.upgrades_enabled:
        raise UpgradeUnavailable("Managed upgrades are disabled")
    if (request.executor_id, request.project) != (principal.subject, settings.upgrade_project):
        raise UpgradeForbidden("Executor claim identity or project differs")
    await operation_lock(session)
    owner = await session.get(UpgradeExecutorRow, request.project, with_for_update=True)
    if owner is None or owner.identity != request.executor_id:
        raise UpgradeUnavailable("Independent Core recovery bootstrap is not ready")
    row = await session.scalar(
        select(UpgradeOperationRow)
        .where(
            UpgradeOperationRow.active_slot == 1,
        )
        .with_for_update()
    )
    if row is None:
        require_fresh_bootstrap(owner)
        return UpgradeClaimIdle()
    operation = UpgradeOperation.model_validate(row.payload)
    if operation.kind is not UpgradeKind.CONTROL_PLANE:
        require_fresh_bootstrap(owner)
        return UpgradeClaimIdle()
    if row.execution_plan is None:
        raise UpgradeConflict("Core operation has no accepted execution plan")
    plan = UpgradeExecutionPlan.model_validate(row.execution_plan)
    if (
        plan.source.manifest.project != request.project
        or plan.operation.authority_sha256() != operation.authority_sha256()
        or operation.source_release_id != owner.source_release_id
        or operation.source_sha256 != request.observed_source_sha256
    ):
        raise UpgradeConflict("Core executor observed Source or operation authority differs")
    if row.executor_identity is not None:
        if row.executor_identity != request.executor_id:
            raise UpgradeConflict("Another Core executor retains recovery ownership")
        # Accepted recovery remains scoped to this exact Source and immutable
        # operation while application containers or the API are restarting.
        # A heartbeat lapse cannot revoke restoration ownership. Grants still
        # perform their separate live human and activation-boundary checks.
        return plan.model_copy(update={"operation": operation})
    require_fresh_bootstrap(owner)
    if not operation.cancel_requested:
        try:
            await authorize(session, initiating_actor(row))
        except UpgradeForbidden:
            # Claiming an already accepted plan also supplies its restricted
            # Source cleanup owner. Revocation prohibits grants, not recovery.
            operation = operation.model_copy(
                update={
                    "cancel_requested": True,
                    "reason": "authorization_revoked",
                    "updated_at": datetime.now(UTC),
                }
            )
            row.payload = operation.model_dump(mode="json")
    row.executor_identity = request.executor_id
    await write_principal_audit(
        session,
        principal=principal,
        action="upgrades.executor.claim",
        target_type="upgrade",
        target_id=operation.job_id,
        outcome=AuditOutcome.OK,
        context={
            "generation": operation.generation,
            "project": request.project,
            "plan_sha256": plan.authority_sha256(),
            "cleanup_only": operation.cancel_requested,
        },
    )
    await session.flush()
    return plan.model_copy(update={"operation": operation})


def require_fresh_bootstrap(owner: UpgradeExecutorRow) -> None:
    if (
        not owner.bootstrap_ready
        or not 0 <= (datetime.now(UTC) - owner.heartbeat_at).total_seconds() <= 10
    ):
        raise UpgradeUnavailable("Independent Core recovery bootstrap is not ready")
