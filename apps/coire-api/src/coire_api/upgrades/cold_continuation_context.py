"""Expired cold predecessor metadata; no ownership or execution budget is issued."""

from datetime import UTC, datetime

from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import (
    UpgradeExpiredRecoveryRow,
    UpgradeOperationRow,
    UpgradeRecoverySuccessionRow,
)
from coire_api.upgrades.expired_recovery_data import (
    capture_expired_metadata_snapshot,
    verify_expired_metadata_preserved,
)
from coire_api.upgrades.grants import verify_fenced
from coire_api.upgrades.persistence import get_release, operation_lock
from coire_api.upgrades.recovery_support_scope import require_original
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryCancellation,
    UpgradeExpiredRecoveryColdClaim,
    UpgradeExpiredRecoveryColdHandbackIntent,
    UpgradeExpiredRecoveryColdHandbackReceipt,
    UpgradeExpiredRecoveryColdSmokeCommand,
    UpgradeExpiredRecoveryDataSnapshot,
    UpgradeExpiredRecoveryIntent,
    UpgradeExpiredRecoveryNativeDataSnapshot,
)
from coire_core.models.upgrade_recovery_continuation import (
    UpgradeColdContinuationContext,
    UpgradeColdContinuationGrant,
    UpgradeColdContinuationRead,
)
from coire_core.models.upgrades import UpgradeExecutionPlan, UpgradeOperation

tracer = trace.get_tracer("coire.api.cold_continuation")


async def load_expired_cold_predecessor(
    session: AsyncSession,
    recovery_id: str,
    read: UpgradeColdContinuationRead,
) -> tuple[
    UpgradeExpiredRecoveryRow, UpgradeExpiredRecoveryIntent, UpgradeExpiredRecoveryColdClaim
]:
    """Caller separately authenticates admin or the exact node before use."""
    await operation_lock(session)
    row = await session.get(UpgradeExpiredRecoveryRow, recovery_id, with_for_update=True)
    if row is None or row.cold_claim_payload is None or row.cancellation_payload is None:
        raise UpgradeUnavailable("Cold continuation has no cancelled retained predecessor")
    intent = UpgradeExpiredRecoveryIntent.model_validate(row.intent_payload)
    owner = UpgradeExpiredRecoveryColdClaim.model_validate(row.cold_claim_payload)
    cancellation = UpgradeExpiredRecoveryCancellation.model_validate(row.cancellation_payload)
    from coire_api.upgrades.cold_unstarted_lineage import unstarted_descendant

    unstarted = await unstarted_descendant(session, row, owner)
    request = intent.request
    require_original(
        request.original_job_id, request.original_generation, request.expected_source_sha256
    )
    if (
        row.id != intent.recovery_id
        or row.original_job_id != request.original_job_id
        or row.intent_sha256 != intent.sha256()
        or read.previous_intent_sha256 != row.intent_sha256
        or read.previous_owner_sha256 != owner.sha256()
        or owner.intent != intent
        or owner.supervision != "fixture"
        or owner.retired_supervisor is None
        or intent.original_plan.operation.node != "coire-edge-a"
        or (unstarted is None and (row.phase != "recovery_required" or row.active_slot != 1))
        or intent.request.recovery_mode != "cold"
        or datetime.now(UTC) < intent.deadline_at
        or cancellation.recovery_id != recovery_id
        or cancellation.intent_sha256 != intent.sha256()
        or cancellation.original_job_id != request.original_job_id
        or cancellation.original_generation != request.original_generation
        or any(
            value is not None
            for value in (
                row.transfer_payload,
                row.consumption_payload,
                row.owner_claim_payload,
                row.resolution_payload if unstarted is None else None,
            )
        )
    ):
        raise UpgradeConflict("Cold continuation predecessor scope or boundary differs")
    handback = row.cold_handback_receipt_payload
    if handback is None:
        if row.cold_handback_intent_payload is not None:
            partial = UpgradeExpiredRecoveryColdHandbackIntent.model_validate(
                row.cold_handback_intent_payload
            )
            if (
                row.cold_verification_payload != partial.verification.model_dump(mode="json")
                or partial.verification.request.smoke.smoke.source.launch.source.projection.owner
                != owner
            ):
                raise UpgradeConflict("Cold continuation partial handback provenance differs")
        elif row.cold_verification_payload is not None:
            raise UpgradeConflict("Cold continuation verification or handback outcome is uncertain")
    else:
        receipt = UpgradeExpiredRecoveryColdHandbackReceipt.model_validate(handback)
        if (
            row.cold_verification_payload
            != receipt.helper.handback.verification.model_dump(mode="json")
            or row.cold_handback_intent_payload != receipt.helper.handback.model_dump(mode="json")
            or receipt.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
            != owner
        ):
            raise UpgradeConflict("Cold continuation committed handback provenance differs")
    original = await session.get(UpgradeOperationRow, request.original_job_id, with_for_update=True)
    if original is None or original.execution_plan is None:
        raise UpgradeUnavailable("Cold continuation original obligation is absent")
    operation = UpgradeOperation.model_validate(original.payload)
    plan = UpgradeExecutionPlan.model_validate(original.execution_plan)
    if (
        original.active_slot != 1
        or original.phase != "recovery_required"
        or operation.phase.value != "recovery_required"
        or operation.finished_at is not None
        or operation.authority_sha256() != plan.operation.authority_sha256()
        or plan.authority_sha256() != intent.original_plan.authority_sha256()
        or owner.original_native.recovery_deadline_at != intent.original_recovery_deadline_at
        or owner.original_native.source.release_sha256 != request.expected_source_sha256
    ):
        raise UpgradeConflict("Cold continuation original failure changed")
    await verify_fenced(session, plan)
    support = await get_release(session, request.support_release_id)
    if support != intent.support_release:
        raise UpgradeConflict("Cold continuation predecessor catalog differs")
    return row, intent, owner


@tracer.start_as_current_span("coire.api.cold_continuation_context", record_exception=False)
async def read_cold_predecessor_context(
    session: AsyncSession,
    recovery_id: str,
    read: UpgradeColdContinuationRead,
    node: str,
) -> UpgradeColdContinuationContext:
    # Check the authenticated node before inspecting mutation-sensitive context.
    row = await session.get(UpgradeExpiredRecoveryRow, recovery_id)
    if row is None:
        raise UpgradeUnavailable("Cold continuation predecessor is absent")
    retained = UpgradeExpiredRecoveryIntent.model_validate(row.intent_payload)
    if node != retained.original_plan.operation.node:
        raise UpgradeForbidden()
    row, intent, owner = await load_expired_cold_predecessor(session, recovery_id, read)
    from coire_api.upgrades.cold_unstarted_lineage import unstarted_descendant

    unstarted = await unstarted_descendant(session, row, owner)
    if row.data_baseline_payload is None or row.native_baseline_payload is None:
        raise UpgradeConflict("Cold continuation has no original preservation baselines")
    before = UpgradeExpiredRecoveryDataSnapshot.model_validate(row.data_baseline_payload)
    native = UpgradeExpiredRecoveryNativeDataSnapshot.model_validate(row.native_baseline_payload)
    # These hashes bound only expired metadata reads; they grant no effects.
    current = await capture_expired_metadata_snapshot(session, intent)
    verify_expired_metadata_preserved(before, current, intent)
    unstarted_grant = None
    if unstarted is not None:
        stored = await session.scalar(
            select(UpgradeRecoverySuccessionRow).where(
                UpgradeRecoverySuccessionRow.recovery_id == unstarted[1].recovery_id
            )
        )
        if stored is None:
            raise UpgradeConflict("Unstarted continuation has no recorded custody grant")
        unstarted_grant = UpgradeColdContinuationGrant.model_validate(stored.grant_payload)
        if (
            unstarted_grant.intent != unstarted[1]
            or unstarted_grant.sha256() != stored.grant_sha256
        ):
            raise UpgradeConflict("Unstarted custody grant changed")
    return UpgradeColdContinuationContext(
        intent=intent,
        owner=owner,
        unstarted_intent=unstarted[1] if unstarted is not None else None,
        unstarted_grant=unstarted_grant,
        previous_smoke_command=(
            UpgradeExpiredRecoveryColdSmokeCommand.model_validate(row.cold_smoke_payload)
            if row.cold_smoke_payload is not None
            else None
        ),
        previous_handback=(
            UpgradeExpiredRecoveryColdHandbackReceipt.model_validate(
                row.cold_handback_receipt_payload
            )
            if row.cold_handback_receipt_payload is not None
            else None
        ),
        previous_handback_intent=(
            UpgradeExpiredRecoveryColdHandbackIntent.model_validate(
                row.cold_handback_intent_payload
            )
            if row.cold_handback_intent_payload is not None
            and row.cold_handback_receipt_payload is None
            else None
        ),
        data_baseline=before,
        native_baseline=native,
        current_data=current,
        observed_at=datetime.now(UTC),
    )
