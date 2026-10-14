"""Audited additive preparation for the positively known legacy 0034 fixture.

No existing row, old deadline or Alembic history is rewritten. Unknown partial
protocols refuse; normal fresh installations use the ordinary migration.
"""

from datetime import UTC, datetime

from opentelemetry import trace
from sqlalchemy import Table, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateTable

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import (
    AuditRow,
    Base,
    UpgradeOperationRow,
)
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.grants import verify_fenced
from coire_api.upgrades.persistence import operation_lock
from coire_api.upgrades.recovery_schema_sql import RECOVERY_IMMUTABILITY_SQL
from coire_api.upgrades.schema_identity import inspect_schema
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeRecoverySchemaObservation,
    UpgradeRecoverySchemaReceipt,
    UpgradeRecoverySchemaRequest,
)
from coire_core.models.upgrades import (
    UpgradeExecutionPlan,
    UpgradeKind,
    UpgradeOperation,
    UpgradePhase,
)

tracer = trace.get_tracer("coire.api.recovery_schema")
TABLES: tuple[Table, ...] = tuple(
    Base.metadata.tables[name]
    for name in (
        "upgrade_expired_recoveries",
        "upgrade_recovery_successions",
        "upgrade_recovery_successor_claims",
    )
)
ACTION = "upgrades.expired_recovery.schema.prepare"


def legacy_checks() -> dict[str, str]:
    phases = [phase.value for phase in UpgradePhase]
    # Exact PostgreSQL deparsed legacy expressions. No regex or caller text is
    # allowed to stand in for the positive old constraint shape.
    phase_array = ", ".join(f"('{phase}'::character varying)::text" for phase in phases)
    terminal_array = ", ".join(
        f"('{phase}'::character varying)::text"
        for phase in ("succeeded", "failed_unchanged", "cancelled_unchanged", "rolled_back")
    )
    return {
        "ck_upgrade_phase": f"CHECK (((phase)::text = ANY (ARRAY[{phase_array}])))",
        "ck_upgrade_terminal_owner": f"CHECK (((active_slot IS NULL) = ((phase)::text = ANY (ARRAY[{terminal_array}]))))",
    }


async def original_context(
    session: AsyncSession, principal: Principal, job_id: str
) -> tuple[UpgradeOperationRow, UpgradeExecutionPlan]:
    await operation_lock(session)
    await authorize(session, principal)
    row = await session.get(UpgradeOperationRow, job_id, with_for_update=True)
    if row is None or row.execution_plan is None:
        raise UpgradeConflict("Legacy preparation original operation is absent")
    operation = UpgradeOperation.model_validate(row.payload)
    plan = UpgradeExecutionPlan.model_validate(row.execution_plan)
    from coire_api.upgrades.recovery_support_scope import require_original

    require_original(plan.operation.job_id, plan.operation.generation, plan.source.manifest_sha256)
    if (
        operation.kind is not UpgradeKind.STUDIO
        or operation.phase is not UpgradePhase.RECOVERY_REQUIRED
        or row.phase != "recovery_required"
        or row.active_slot != 1
        or operation.finished_at is not None
        or operation.authority_sha256() != plan.operation.authority_sha256()
    ):
        raise UpgradeConflict("Legacy preparation has no exact retained Studio failure")
    await verify_fenced(session, plan)
    return row, plan


async def observe_schema(
    session: AsyncSession, plan: UpgradeExecutionPlan
) -> UpgradeRecoverySchemaObservation:
    head, digest = await inspect_schema(session)
    if head != "0034_upgrades":
        raise UpgradeConflict("Legacy preparation requires the known 0034 revision")
    names = set(
        (
            await session.scalars(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() "
                    "AND table_name IN ('upgrade_expired_recoveries','upgrade_recovery_successions','upgrade_recovery_successor_claims')"
                )
            )
        ).all()
    )
    if names and names != {table.name for table in TABLES}:
        raise UpgradeConflict("Legacy preparation refuses a partial recovery protocol")
    return UpgradeRecoverySchemaObservation(
        original_job_id=plan.operation.job_id,
        original_generation=plan.operation.generation,
        original_plan_sha256=plan.authority_sha256(),
        source_sha256=plan.source.manifest_sha256,
        schema_sha256=digest,
        recovery_tables_absent=not names,
        observed_at=datetime.now(UTC),
    )


@tracer.start_as_current_span("coire.api.recovery_schema_observe")
async def inspect_legacy_schema(
    session: AsyncSession, principal: Principal, job_id: str
) -> UpgradeRecoverySchemaObservation:
    _, plan = await original_context(session, principal, job_id)
    return await observe_schema(session, plan)


@tracer.start_as_current_span("coire.api.recovery_schema_prepare")
async def prepare_legacy_schema(
    session: AsyncSession, principal: Principal, request: UpgradeRecoverySchemaRequest
) -> tuple[UpgradeRecoverySchemaReceipt, bool]:
    row, plan = await original_context(session, principal, request.original_job_id)
    actor = await authorize(session, principal)
    if (
        request.original_generation != plan.operation.generation
        or request.original_plan_sha256 != plan.authority_sha256()
        or request.expected_source_sha256 != plan.source.manifest_sha256
    ):
        raise UpgradeConflict("Legacy preparation original scope differs")
    previous = await session.scalar(
        select(AuditRow).where(
            AuditRow.action == ACTION,
            AuditRow.target_id == request.original_job_id,
            AuditRow.detail["idempotency_key"].astext == request.idempotency_key,
        )
    )
    if previous is not None:
        receipt = UpgradeRecoverySchemaReceipt.model_validate(previous.detail["receipt"])
        if receipt.request != request:
            raise UpgradeConflict("Legacy preparation retry changes captured request")
        return receipt, False
    await session.execute(text("SET LOCAL lock_timeout = '1s'"))
    await session.execute(text("SET LOCAL statement_timeout = '5s'"))
    before = await observe_schema(session, plan)
    if before.schema_sha256 != request.expected_schema_sha256:
        raise UpgradeConflict("Legacy preparation has no positively matching absent protocol")
    if not before.recovery_tables_absent:
        from coire_api.upgrades.recovery_unstarted_schema import (
            prepare_cancelled_unstarted_constraint,
        )

        await prepare_cancelled_unstarted_constraint(session)
        after = await observe_schema(session, plan)
        receipt = UpgradeRecoverySchemaReceipt(
            request=request,
            before=before,
            after=after,
            prepared_by=actor,
            prepared_at=datetime.now(UTC),
            preparation_kind="cancelled_unstarted_constraint",
        )
        await write_principal_audit(
            session,
            principal=principal,
            action=ACTION,
            target_type="upgrade_recovery_schema",
            target_id=request.original_job_id,
            detail={
                "idempotency_key": request.idempotency_key,
                "receipt": receipt.model_dump(mode="json"),
                "original_maintenance_retained": True,
                "alembic_history_unchanged": True,
            },
        )
        await session.flush()
        return receipt, True
    checks = {
        str(name): str(definition)
        for name, definition in (
            await session.execute(
                text(
                    "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid=to_regclass('upgrade_operations') AND conname IN ('ck_upgrade_phase','ck_upgrade_terminal_owner')"
                )
            )
        ).all()
    }
    if checks != legacy_checks():
        raise UpgradeConflict("Legacy preparation operation constraints are not the known preimage")
    routines = (
        await session.scalars(
            text(
                "SELECT proname FROM pg_proc WHERE pronamespace=to_regnamespace(current_schema()) "
                "AND proname IN ('expired_recovery_immutable_authority','recovery_succession_immutable_grant','recovery_successor_immutable_claim')"
            )
        )
    ).all()
    if routines:
        raise UpgradeConflict(
            "Legacy preparation found unknown existing recovery authority routines"
        )
    original_payload = dict(row.payload)
    original_plan = dict(row.execution_plan or {})
    for table in TABLES:
        await session.execute(CreateTable(table))
    for statement in RECOVERY_IMMUTABILITY_SQL:
        await session.execute(text(statement))
    await session.refresh(row)
    if (
        row.payload != original_payload
        or row.execution_plan != original_plan
        or row.active_slot != 1
    ):
        raise UpgradeConflict("Legacy preparation changed retained original authority")
    after = await observe_schema(session, plan)
    if after.recovery_tables_absent:
        raise UpgradeUnavailable("Legacy preparation failed to produce recovery protocol tables")
    receipt = UpgradeRecoverySchemaReceipt(
        request=request,
        before=before,
        after=after,
        prepared_by=actor,
        prepared_at=datetime.now(UTC),
    )
    await write_principal_audit(
        session,
        principal=principal,
        action=ACTION,
        target_type="upgrade_recovery_schema",
        target_id=request.original_job_id,
        detail={
            "idempotency_key": request.idempotency_key,
            "receipt": receipt.model_dump(mode="json"),
            "original_maintenance_retained": True,
            "alembic_history_unchanged": True,
        },
    )
    await session.flush()
    return receipt, True
