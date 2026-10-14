"""Database-backed, non-replayable one-shot migration intent and terminal schema proof."""

from datetime import UTC, datetime, timedelta

from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import UpgradeGrantRow, UpgradeReceiptRow
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.compatibility import require_migration_backup
from coire_api.upgrades.grants import initiating_actor, verify_fenced
from coire_api.upgrades.persistence import get_operation, operation_lock
from coire_api.upgrades.schema_identity import inspect_schema
from coire_core.errors import UpgradeConflict, UpgradeForbidden
from coire_core.models.audit import AuditOutcome
from coire_core.models.upgrades import (
    UpgradeExecutionPlan,
    UpgradeKind,
    UpgradeMigrationExecution,
    UpgradeMigrationNotStarted,
    UpgradeOperation,
    UpgradePhase,
    UpgradeReceipt,
)
from coire_core.settings import Settings

tracer = trace.get_tracer("coire.api.upgrades.migration")


@tracer.start_as_current_span(
    "coire.api.upgrade_migration_fence", record_exception=False, set_status_on_exception=False
)
async def fence_not_started(
    session: AsyncSession,
    plan: UpgradeExecutionPlan,
    principal: Principal,
    settings: Settings,
) -> UpgradeMigrationNotStarted:
    """Serialize against the runner's pre-DDL claim and permanently revoke this attempt.

    An absent execution observation alone proves nothing. This transaction prevents
    any delayed runner from claiming, then records the unchanged physical schema.
    The executor still must verify Source, candidate absence and original deadlines.
    """
    await operation_lock(session)
    row = await get_operation(session, plan.operation.job_id, lock=True)
    operation = UpgradeOperation.model_validate(row.payload)
    if (
        not settings.upgrades_enabled
        or not plan.needs_migration
        or plan.operation.kind is not UpgradeKind.CONTROL_PLANE
        or row.execution_plan is None
        or UpgradeExecutionPlan.model_validate(row.execution_plan).authority_sha256()
        != plan.authority_sha256()
        or operation.authority_sha256() != plan.operation.authority_sha256()
        or operation.finished_at is not None
        or row.executor_identity != settings.upgrade_executor_id
        or principal.subject != settings.upgrade_executor_id
    ):
        raise UpgradeConflict("Migration fence differs from accepted recovery authority")
    if row.migration_execution is not None:
        if row.migration_execution.get("state") != "not_started":
            raise UpgradeConflict("A claimed migration cannot be declared not started")
        previous = UpgradeMigrationNotStarted.model_validate(row.migration_execution)
        if (previous.job_id, previous.generation, previous.plan_sha256) != (
            operation.job_id,
            operation.generation,
            plan.authority_sha256(),
        ):
            raise UpgradeConflict("Retained migration fence authority differs")
        return previous
    grant = await session.scalar(
        select(UpgradeGrantRow).where(
            UpgradeGrantRow.job_id == operation.job_id,
            UpgradeGrantRow.generation == operation.generation,
            UpgradeGrantRow.boundary == "migration",
        )
    )
    if grant is None or grant.consumed_at is None:
        raise UpgradeConflict("Migration fence requires its consumed activation")
    await verify_fenced(session, plan)
    observed_at = datetime.now(UTC)
    head, digest = await inspect_schema(session)
    if (head, digest) != (plan.initial_schema_head, plan.initial_schema_sha256):
        raise UpgradeConflict("Unstarted migration Source schema changed")
    proof = UpgradeMigrationNotStarted(
        job_id=operation.job_id,
        generation=operation.generation,
        plan_sha256=plan.authority_sha256(),
        grant_id=grant.id,
        schema_head=head,
        schema_sha256=digest,
        observed_at=observed_at,
    )
    row.migration_execution = proof.model_dump(mode="json")
    await write_principal_audit(
        session,
        principal=principal,
        action="upgrades.migration.not_started",
        target_type="upgrade",
        target_id=operation.job_id,
        outcome=AuditOutcome.OK,
        context={"generation": operation.generation, "plan_sha256": plan.authority_sha256()},
    )
    await session.flush()
    return proof


@tracer.start_as_current_span(
    "coire.api.upgrade_migration_claim", record_exception=False, set_status_on_exception=False
)
async def claim_migration(
    session: AsyncSession,
    job_id: str,
    generation: int,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> tuple[UpgradeExecutionPlan, UpgradeMigrationExecution]:
    """Commit this intent before Alembic. A lost intent never authorizes a second run."""
    now = now or datetime.now(UTC)
    if not settings.upgrades_enabled:
        raise UpgradeForbidden("Managed migration is disabled")
    await operation_lock(session)
    row = await get_operation(session, job_id, lock=True)
    if row.execution_plan is None:
        raise UpgradeConflict("Managed migration has no accepted plan")
    plan = UpgradeExecutionPlan.model_validate(row.execution_plan)
    operation = UpgradeOperation.model_validate(row.payload)
    if (
        plan.operation.kind is not UpgradeKind.CONTROL_PLANE
        or not plan.needs_migration
        or plan.source.manifest.project != settings.upgrade_project
        or (operation.job_id, operation.generation) != (job_id, generation)
        or operation.authority_sha256() != plan.operation.authority_sha256()
    ):
        raise UpgradeConflict("Managed migration authority differs")
    if row.migration_execution is not None:
        if row.migration_execution.get("state") == "not_started":
            fenced = UpgradeMigrationNotStarted.model_validate(row.migration_execution)
            if (fenced.job_id, fenced.generation, fenced.plan_sha256) != (
                job_id,
                generation,
                plan.authority_sha256(),
            ):
                raise UpgradeConflict("Migration execution fence authority differs")
            raise UpgradeConflict("Migration execution was durably fenced before starting")
        previous = UpgradeMigrationExecution.model_validate(row.migration_execution)
        if (previous.job_id, previous.generation, previous.plan_sha256) != (
            job_id,
            generation,
            plan.authority_sha256(),
        ):
            raise UpgradeConflict("Migration execution replay authority differs")
        if previous.state != "complete":
            raise UpgradeConflict("Migration uncertainty forbids automatic re-execution")
        return plan, previous
    if (
        operation.phase is not UpgradePhase.MIGRATING
        or operation.cancel_requested
        or operation.finished_at is not None
        or row.executor_identity != settings.upgrade_executor_id
    ):
        raise UpgradeConflict("Managed migration is outside its accepted activation boundary")
    grant = await session.scalar(
        select(UpgradeGrantRow).where(
            UpgradeGrantRow.job_id == job_id,
            UpgradeGrantRow.generation == generation,
            UpgradeGrantRow.boundary == "migration",
        )
    )
    proof_row = await session.scalar(
        select(UpgradeReceiptRow)
        .where(
            UpgradeReceiptRow.job_id == job_id,
            UpgradeReceiptRow.generation == generation,
        )
        .order_by(UpgradeReceiptRow.sequence.desc())
        .limit(1)
    )
    proof = UpgradeReceipt.model_validate(proof_row.payload) if proof_row is not None else None
    if (
        grant is None
        or grant.consumed_at is None
        or grant.consumed_at > now
        or grant.consumed_at >= grant.expires_at
        or proof is None
        or proof.phase is not UpgradePhase.MIGRATING
        or proof.outcome != "intent"
        or proof.observed_sha256 != plan.operation.source_sha256
        or proof.occurred_at > grant.consumed_at
        or not 0 <= (grant.consumed_at - proof.occurred_at).total_seconds() <= 30
    ):
        raise UpgradeConflict("Managed migration lacks its consumed durable activation")
    deadline = proof.occurred_at + timedelta(seconds=plan.operation.timing.readiness_s)
    if now >= deadline:
        raise UpgradeForbidden("Managed migration captured deadline has elapsed")
    await authorize(session, initiating_actor(row))
    await verify_fenced(session, plan)
    require_migration_backup(plan, now=now)
    head, digest = await inspect_schema(session)
    if (head, digest) != (plan.initial_schema_head, plan.initial_schema_sha256):
        raise UpgradeConflict("Managed migration Source schema changed")
    execution = UpgradeMigrationExecution(
        job_id=job_id,
        generation=generation,
        plan_sha256=plan.authority_sha256(),
        grant_id=grant.id,
        state="intent",
        captured_at=proof.occurred_at,
        deadline_at=deadline,
        schema_before_head=head,
        schema_before_sha256=digest,
        expected_schema_head=plan.schema_head_after or "",
    )
    row.migration_execution = execution.model_dump(mode="json")
    await write_principal_audit(
        session,
        principal=initiating_actor(row),
        action="upgrades.migration.intent",
        target_type="upgrade",
        target_id=job_id,
        outcome=AuditOutcome.OK,
        context={"generation": generation, "plan_sha256": plan.authority_sha256()},
    )
    await session.flush()
    return plan, execution


@tracer.start_as_current_span(
    "coire.api.upgrade_migration_record", record_exception=False, set_status_on_exception=False
)
async def record_migration(
    session: AsyncSession,
    plan: UpgradeExecutionPlan,
    intent: UpgradeMigrationExecution,
    *,
    exit_code: int,
    now: datetime | None = None,
) -> UpgradeMigrationExecution:
    now = now or datetime.now(UTC)
    await operation_lock(session)
    row = await get_operation(session, intent.job_id, lock=True)
    if (
        row.execution_plan is None
        or (
            UpgradeExecutionPlan.model_validate(row.execution_plan).authority_sha256()
            != plan.authority_sha256()
        )
        or row.migration_execution != intent.model_dump(mode="json")
    ):
        raise UpgradeConflict("Migration completion has no matching retained intent")
    head, digest = await inspect_schema(session)
    complete = exit_code == 0 and head == intent.expected_schema_head and now <= intent.deadline_at
    result = UpgradeMigrationExecution.model_validate(
        intent.model_dump()
        | {
            "state": "complete" if complete else "uncertain",
            "exit_code": exit_code,
            "schema_after_head": head,
            "schema_after_sha256": digest,
            "completed_at": now,
        }
    )
    row.migration_execution = result.model_dump(mode="json")
    await write_principal_audit(
        session,
        principal=initiating_actor(row),
        action="upgrades.migration.observed",
        target_type="upgrade",
        target_id=intent.job_id,
        outcome=AuditOutcome.OK,
        context={
            "generation": intent.generation,
            "state": result.state,
            "exit_code": exit_code,
            "schema_head": head,
            "schema_sha256": digest,
        },
    )
    await session.flush()
    return result
