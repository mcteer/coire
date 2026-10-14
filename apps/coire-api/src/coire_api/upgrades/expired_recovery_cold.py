"""Audited cold-owner ledger, distinct from live-preservation transfer authority."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Literal

from opentelemetry import metrics, trace
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.db import UpgradeExpiredRecoveryRow
from coire_api.upgrades.expired_recovery_owner import verify_intent_binding
from coire_api.upgrades.persistence import operation_lock
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdClaim,
    UpgradeExpiredRecoveryColdSmokeCommand,
    UpgradeExpiredRecoveryColdState,
    UpgradeExpiredRecoveryDataSnapshot,
    UpgradeExpiredRecoveryIntent,
)
from coire_core.models.upgrades import NodeAgentProcessIdentity

tracer = trace.get_tracer("coire.api.cold_recovery")
logger = logging.getLogger(__name__)
claims = metrics.get_meter("coire.api.cold_recovery").create_counter(
    "coire_upgrade_cold_recovery_claims_total"
)
transitions = metrics.get_meter("coire.api.cold_recovery").create_counter(
    "coire_upgrade_cold_recovery_transitions_total"
)


def record_cold_transition(
    claim: UpgradeExpiredRecoveryColdClaim,
    phase: Literal["source_verified", "handback_prepared", "handed_back", "recovered"],
    created: bool,
) -> None:
    """Only committed transitions count; replay never reports another effect."""
    if created:
        transitions.add(1, {"node": claim.original_claim.node, "phase": phase})
        logger.info(
            "cold recovery transition committed",
            extra={
                "job_id": claim.original_claim.job_id,
                "recovery_id": claim.intent.recovery_id,
                "user_id": str(claim.intent.accepted_by),
                "model_id": str(claim.intent.original_plan.model_id),
                "node": claim.original_claim.node,
                "phase": phase,
            },
        )


def record_cold_claim(claim: UpgradeExpiredRecoveryColdClaim, created: bool) -> None:
    """Called after committed audit, with bounded labels and scoped identifiers."""
    if created:
        claims.add(1, {"node": claim.original_claim.node, "outcome": "accepted"})
        logger.info(
            "cold recovery owner accepted",
            extra={
                "job_id": claim.intent.request.original_job_id,
                "recovery_id": claim.intent.recovery_id,
                "generation": claim.original_claim.generation,
                "user_id": str(claim.intent.accepted_by),
                "node": claim.original_claim.node,
            },
        )


async def pending_binding(
    session: AsyncSession, recovery_id: str, node: str
) -> tuple[UpgradeExpiredRecoveryRow, UpgradeExpiredRecoveryIntent]:
    await operation_lock(session)
    row = await session.get(UpgradeExpiredRecoveryRow, recovery_id, with_for_update=True)
    if row is None:
        raise UpgradeUnavailable("Cold recovery authority is absent")
    intent = UpgradeExpiredRecoveryIntent.model_validate(row.intent_payload)
    await verify_intent_binding(session, row, intent, node)
    if intent.request.recovery_mode != "cold" or any(
        value is not None
        for value in (row.transfer_payload, row.consumption_payload, row.owner_claim_payload)
    ):
        raise UpgradeConflict("Cold recovery cannot borrow live-preservation ownership")
    from coire_api.upgrades.cold_continuation_binding import retained_binding

    grant = await retained_binding(session, intent)
    if row.cold_claim_payload is not None:
        accepted = UpgradeExpiredRecoveryColdClaim.model_validate(row.cold_claim_payload)
        if accepted.continuation != (grant.binding if grant is not None else None):
            raise UpgradeConflict("Cold owner changes its immutable continuation binding")
    return row, intent


@tracer.start_as_current_span("coire.api.cold_recovery_claim")
async def accept_cold_claim(
    session: AsyncSession, claim: UpgradeExpiredRecoveryColdClaim, node: str
) -> tuple[UpgradeExpiredRecoveryColdClaim, bool]:
    """Trusted native producer must independently hold actual physical exclusion.

    Ledger acceptance alone is not permission from absence, Source serving or
    cleanup qualification. Replay retains its original history without renewal.
    """
    claim = UpgradeExpiredRecoveryColdClaim.model_validate_json(claim.model_dump_json())
    row, intent = await pending_binding(session, claim.intent.recovery_id, node)
    if claim.intent != intent:
        raise UpgradeConflict("Cold claim differs from captured recovery intent")
    if row.cold_claim_payload is not None:
        previous = UpgradeExpiredRecoveryColdClaim.model_validate(row.cold_claim_payload)
        if previous != claim:
            raise UpgradeConflict("Cold recovery retry changes the captured owner")
        return previous, False
    from coire_api.upgrades.cold_continuation_binding import retained_binding

    grant = await retained_binding(session, intent)
    if claim.continuation != (grant.binding if grant is not None else None):
        raise UpgradeConflict("Cold claim changes its immutable continuation binding")
    now = datetime.now(UTC)
    if (
        row.active_slot != 1
        or row.phase != "accepted"
        or not claim.claimed_at
        <= now
        < min(intent.deadline_at, claim.claimed_at + timedelta(seconds=30))
        or not intent.accepted_at <= now < intent.deadline_at
    ):
        raise UpgradeUnavailable("Cold recovery claim is not live or fresh")
    actor = await verify_intent_binding(session, row, intent, node)
    row.cold_claim_payload = claim.model_dump(mode="json")
    row.phase = "cold_recovering"
    await write_principal_audit(
        session,
        principal=actor,
        action="upgrades.expired_recovery.cold_owner.accept",
        target_type="upgrade_recovery",
        target_id=intent.recovery_id,
        detail={"original_job_id": row.original_job_id, "owner_sha256": claim.sha256()},
    )
    await session.flush()
    return claim, True


@tracer.start_as_current_span("coire.api.cold_recovery_check")
async def verify_cold_owner(
    session: AsyncSession,
    recovery_id: str,
    claim_sha256: str,
    process: NodeAgentProcessIdentity,
    node: str,
) -> UpgradeExpiredRecoveryColdClaim:
    row, intent = await pending_binding(session, recovery_id, node)
    if row.cold_claim_payload is None:
        raise UpgradeUnavailable("Cold owner has no accepted claim")
    claim = UpgradeExpiredRecoveryColdClaim.model_validate(row.cold_claim_payload)
    if claim.sha256() != claim_sha256 or claim.process != process or claim.intent != intent:
        raise UpgradeForbidden()
    if (
        row.active_slot != 1
        or row.phase != "cold_recovering"
        or not claim.claimed_at <= datetime.now(UTC) < intent.deadline_at
    ):
        raise UpgradeUnavailable("Cold owner captured authority is exhausted")
    return claim


async def probe_cold_owner(
    session: AsyncSession,
    recovery_id: str,
    claim_sha256: str,
    process: NodeAgentProcessIdentity,
    node: str,
) -> UpgradeExpiredRecoveryColdState:
    row, intent = await pending_binding(session, recovery_id, node)
    if row.cold_claim_payload is not None:
        owner = await verify_cold_owner(session, recovery_id, claim_sha256, process, node)
    else:
        if (
            row.active_slot != 1
            or row.phase != "accepted"
            or not intent.accepted_at <= datetime.now(UTC) < intent.deadline_at
        ):
            raise UpgradeUnavailable("Cold initial authority is not live")
        if process.environment_sha256 != intent.support_release.manifest.environment_sha256:
            raise UpgradeForbidden()
        owner = None
    return UpgradeExpiredRecoveryColdState(
        claim_sha256=claim_sha256, owner=owner, checked_at=datetime.now(UTC)
    )


@tracer.start_as_current_span("coire.api.cold_recovery_smoke_gate")
async def authorize_cold_smoke(
    session: AsyncSession, smoke: UpgradeExpiredRecoveryColdSmokeCommand, node: str
) -> tuple[UpgradeExpiredRecoveryColdSmokeCommand, bool]:
    from coire_api.upgrades.service import smoke_asset

    smoke = UpgradeExpiredRecoveryColdSmokeCommand.model_validate_json(smoke.model_dump_json())
    claim = smoke.source.launch.source.projection.owner
    await verify_cold_owner(session, claim.intent.recovery_id, claim.sha256(), claim.process, node)
    plan = claim.intent.original_plan
    asset = await smoke_asset(session, plan.model_id, (node,))
    if asset != (plan.model_id, plan.model_variant_id, plan.model_slug, plan.model_manifest_sha256):
        raise UpgradeConflict("Cold smoke registry identity changed after accepted preparation")
    row, _ = await pending_binding(session, claim.intent.recovery_id, node)
    if row.data_baseline_payload is None or row.native_baseline_payload is None:
        raise UpgradeUnavailable("Cold Source smoke has no retained pre-effect data baseline")
    baseline = UpgradeExpiredRecoveryDataSnapshot.model_validate(row.data_baseline_payload)
    from coire_api.upgrades.expired_recovery_data import verify_data_preserved

    verify_data_preserved(baseline, baseline, claim.intent)
    if row.cold_smoke_payload is not None:
        retained = UpgradeExpiredRecoveryColdSmokeCommand.model_validate(row.cold_smoke_payload)
        if retained != smoke:
            raise UpgradeConflict("Cold Source smoke retry changes captured authority")
        return retained, False
    if not claim.claimed_at <= smoke.captured_at <= datetime.now(UTC) < claim.intent.deadline_at:
        raise UpgradeUnavailable("Cold Source smoke captured authority is not live")
    actor = await verify_intent_binding(session, row, claim.intent, node)
    row.cold_smoke_payload = smoke.model_dump(mode="json")
    await write_principal_audit(
        session,
        principal=actor,
        action="upgrades.expired_recovery.cold_smoke.authorize",
        target_type="upgrade_recovery",
        target_id=claim.intent.recovery_id,
        detail={
            "original_job_id": row.original_job_id,
            "smoke_sha256": smoke.sha256(),
            "model_id": str(plan.model_id),
            "model_variant_id": str(plan.model_variant_id),
        },
    )
    await session.flush()
    return smoke, True
