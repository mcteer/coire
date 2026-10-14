"""Audit explicit cancellation before a support producer touches an unpublished namespace."""

from datetime import UTC, datetime

from opentelemetry import metrics, trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import AuditRow
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.persistence import get_operation, operation_lock
from coire_api.upgrades.recovery_support_scope import scope
from coire_core.errors import UpgradeConflict, UpgradeForbidden
from coire_core.models.upgrade_unclaimed import (
    UpgradeUnclaimedCancellationAudit,
    UpgradeUnclaimedCancellationGrant,
    UpgradeUnclaimedCancellationRequest,
    UpgradeUnclaimedCancellationState,
)
from coire_core.models.upgrades import UpgradeExecutionPlan, UpgradeOperation, UpgradePhase

ACTION = "upgrades.unclaimed_preparation.cancel.request"
tracer = trace.get_tracer("coire.api.unclaimed_cancellation")
requests = metrics.get_meter("coire.api.unclaimed_cancellation").create_counter(
    "coire_upgrade_unclaimed_cancellation_requests_total"
)


async def unstarted_plan(session: AsyncSession, job_id: str) -> UpgradeExecutionPlan:
    row = await get_operation(session, job_id, lock=True)
    operation = UpgradeOperation.model_validate(row.payload)
    if (
        operation.phase is not UpgradePhase.ACCEPTED
        or operation.receipt_sequence != 0
        or operation.finished_at is not None
        or row.active_slot != 1
        or row.node_command is not None
        or row.execution_plan is None
    ):
        raise UpgradeConflict("Cancellation requires an accepted operation without activation")
    plan = UpgradeExecutionPlan.model_validate(row.execution_plan)
    scoped = scope.get()
    if scoped is not None and plan.operation.source_sha256 != scoped[2]:
        raise UpgradeForbidden("Cancellation preparation has another scoped Source")
    return plan


@tracer.start_as_current_span("coire.api.unclaimed_cancellation", record_exception=False)
async def request_unclaimed_cancellation(
    session: AsyncSession,
    principal: Principal,
    job_id: str,
    request: UpgradeUnclaimedCancellationRequest,
) -> UpgradeUnclaimedCancellationGrant:
    await operation_lock(session)
    actor = await authorize(session, principal)
    plan = await unstarted_plan(session, job_id)
    if plan.authority_sha256() != request.plan_sha256:
        raise UpgradeConflict("Cancellation preparation plan differs")
    existing = await session.scalar(
        select(AuditRow).where(AuditRow.action == ACTION, AuditRow.target_id == job_id)
    )
    if existing is not None:
        captured = UpgradeUnclaimedCancellationAudit.model_validate(existing.detail["authority"])
        grant = UpgradeUnclaimedCancellationGrant(
            plan=plan, requested_by=captured.requested_by, requested_at=captured.requested_at
        )
        if captured.plan_sha256 != plan.authority_sha256():
            raise UpgradeConflict("Cancellation preparation authority changed")
        return grant
    grant = UpgradeUnclaimedCancellationGrant(
        plan=plan, requested_by=actor, requested_at=datetime.now(UTC)
    )
    row = await get_operation(session, job_id, lock=True)
    current = UpgradeOperation.model_validate(row.payload)
    row.payload = current.model_copy(
        update={
            "cancel_requested": True,
            "reason": "administrator_cancelled",
            "updated_at": datetime.now(UTC),
        }
    ).model_dump(mode="json")
    await write_principal_audit(
        session,
        principal=principal,
        action=ACTION,
        target_type="upgrade",
        target_id=job_id,
        detail={
            "authority": UpgradeUnclaimedCancellationAudit(
                plan_sha256=plan.authority_sha256(),
                requested_by=grant.requested_by,
                requested_at=grant.requested_at,
            ).model_dump(mode="json"),
            "activation_authorized": False,
        },
    )
    await session.flush()
    return grant


async def read_unclaimed_cancellation(
    session: AsyncSession, job_id: str, node: str
) -> UpgradeUnclaimedCancellationState:
    await operation_lock(session)
    plan = await unstarted_plan(session, job_id)
    if plan.operation.node != node:
        raise UpgradeForbidden("Cancellation preparation targets another Studio")
    audit = await session.scalar(
        select(AuditRow).where(AuditRow.action == ACTION, AuditRow.target_id == job_id)
    )
    if audit is None:
        raise UpgradeForbidden("Cancellation preparation has no audited admin request")
    captured = UpgradeUnclaimedCancellationAudit.model_validate(audit.detail["authority"])
    grant = UpgradeUnclaimedCancellationGrant(
        plan=plan, requested_by=captured.requested_by, requested_at=captured.requested_at
    )
    row = await get_operation(session, job_id, lock=True)
    if captured.plan_sha256 != plan.authority_sha256() or not row.payload.get("cancel_requested"):
        raise UpgradeConflict("Cancellation preparation authority changed")
    return UpgradeUnclaimedCancellationState(grant=grant, observed_at=datetime.now(UTC))
