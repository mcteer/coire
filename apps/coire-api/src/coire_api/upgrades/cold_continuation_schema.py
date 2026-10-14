"""Audited exact schema extension for truthful separate recovery supersession."""

import hashlib
from datetime import UTC, datetime

from opentelemetry import trace
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import AuditRow, UpgradeExpiredRecoveryRow
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.cold_continuation_context import load_expired_cold_predecessor
from coire_api.upgrades.cold_continuation_schema_checks import continuation_ddl, known_checks
from coire_api.upgrades.persistence import operation_lock
from coire_api.upgrades.recovery_support_scope import require_original
from coire_api.upgrades.schema_identity import inspect_schema
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryIntent
from coire_core.models.upgrade_recovery_continuation import (
    UpgradeColdContinuationRead,
    UpgradeColdContinuationSchemaObservation,
    UpgradeColdContinuationSchemaReceipt,
    UpgradeColdContinuationSchemaRequest,
)

tracer = trace.get_tracer("coire.api.cold_continuation_schema")
ACTION = "upgrades.expired_recovery.continuation.schema.prepare"


async def inspect_checks(session: AsyncSession) -> bool:
    rows = await session.execute(
        text(
            "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid=to_regclass('upgrade_expired_recoveries') AND "
            "conname IN ('ck_expired_recovery_phase','ck_expired_recovery_terminal_owner',"
            "'ck_cold_recovery_supersession_payload','ck_expired_recovery_cancel_retained')"
        )
    )
    actual = {str(name): str(definition) for name, definition in rows.all()}
    if actual == known_checks(continuation=True):
        return True
    if actual == known_checks(continuation=True, unstarted=False):
        return False
    if actual == known_checks(continuation=False):
        return False
    raise UpgradeConflict("Cold continuation refuses unknown or partial recovery constraints")


@tracer.start_as_current_span("coire.api.cold_continuation_schema_observe", record_exception=False)
async def observe_schema(
    session: AsyncSession, principal: Principal, recovery_id: str, read: UpgradeColdContinuationRead
) -> UpgradeColdContinuationSchemaObservation:
    await authorize(session, principal)
    row, intent, owner = await load_expired_cold_predecessor(session, recovery_id, read)
    head, digest = await inspect_schema(session)
    if head != "0034_upgrades":
        raise UpgradeConflict("Cold continuation requires the known 0034 recovery schema")
    return UpgradeColdContinuationSchemaObservation(
        previous_recovery_id=row.id,
        previous_intent_sha256=intent.sha256(),
        previous_owner_sha256=owner.sha256(),
        original_job_id=row.original_job_id,
        original_generation=intent.request.original_generation,
        schema_sha256=digest,
        continuation_state_ready=await inspect_checks(session),
        observed_at=datetime.now(UTC),
    )


@tracer.start_as_current_span("coire.api.cold_continuation_schema_prepare", record_exception=False)
async def prepare_schema(
    session: AsyncSession, principal: Principal, request: UpgradeColdContinuationSchemaRequest
) -> tuple[UpgradeColdContinuationSchemaReceipt, bool]:
    await operation_lock(session)
    actor = await authorize(session, principal)
    row = await session.get(UpgradeExpiredRecoveryRow, request.previous_recovery_id)
    if row is None:
        raise UpgradeUnavailable("Cold continuation schema predecessor is absent")
    intent = UpgradeExpiredRecoveryIntent.model_validate(row.intent_payload)
    require_original(
        intent.request.original_job_id,
        intent.request.original_generation,
        intent.request.expected_source_sha256,
    )
    if row.intent_sha256 != intent.sha256() or row.intent_sha256 != request.previous_intent_sha256:
        raise UpgradeConflict("Cold continuation schema predecessor differs")
    previous = await session.scalar(
        select(AuditRow).where(
            AuditRow.action == ACTION,
            AuditRow.target_id == request.previous_recovery_id,
            AuditRow.detail["request_sha256"].astext == request.sha256(),
        )
    )
    if previous is not None:
        receipt = UpgradeColdContinuationSchemaReceipt.model_validate(previous.detail["receipt"])
        if (
            receipt.request_sha256 != request.sha256()
            or receipt.previous_recovery_id != row.id
            or receipt.previous_intent_sha256 != row.intent_sha256
            or receipt.previous_owner_sha256 != request.previous_owner_sha256
            or receipt.original_job_id != intent.request.original_job_id
            or receipt.original_generation != intent.request.original_generation
        ):
            raise UpgradeConflict("Cold continuation schema retained receipt differs")
        return receipt, False
    idempotency_digest = hashlib.sha256(request.idempotency_key.encode()).hexdigest()
    conflict = await session.scalar(
        select(AuditRow).where(
            AuditRow.action == ACTION,
            AuditRow.target_id == request.previous_recovery_id,
            AuditRow.detail["idempotency_sha256"].astext == idempotency_digest,
        )
    )
    if conflict is not None:
        raise UpgradeConflict("Cold continuation schema retry changes its captured request")
    read = UpgradeColdContinuationRead(
        previous_intent_sha256=request.previous_intent_sha256,
        previous_owner_sha256=request.previous_owner_sha256,
    )
    await session.execute(text("SET LOCAL lock_timeout = '1s'"))
    await session.execute(text("SET LOCAL statement_timeout = '5s'"))
    before = await observe_schema(session, principal, request.previous_recovery_id, read)
    if before.schema_sha256 != request.expected_schema_sha256:
        raise UpgradeConflict("Cold continuation schema changed before preparation")
    if not before.continuation_state_ready:
        for statement in continuation_ddl():
            await session.execute(text(statement))
    after = await observe_schema(session, principal, request.previous_recovery_id, read)
    if not after.continuation_state_ready:
        raise UpgradeUnavailable("Cold continuation schema extension remains uncertain")
    receipt = UpgradeColdContinuationSchemaReceipt(
        request_sha256=request.sha256(),
        previous_recovery_id=row.id,
        previous_intent_sha256=row.intent_sha256,
        previous_owner_sha256=request.previous_owner_sha256,
        original_job_id=before.original_job_id,
        original_generation=before.original_generation,
        before_schema_sha256=before.schema_sha256,
        after_schema_sha256=after.schema_sha256,
        prepared_by=actor,
        prepared_at=datetime.now(UTC),
    )
    await write_principal_audit(
        session,
        principal=principal,
        action=ACTION,
        target_type="upgrade_recovery",
        target_id=row.id,
        detail={
            "request_sha256": request.sha256(),
            "idempotency_sha256": idempotency_digest,
            "receipt": receipt.model_dump(mode="json"),
            "original_maintenance_retained": True,
        },
    )
    await session.flush()
    return receipt, True
