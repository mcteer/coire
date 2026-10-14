"""Atomic separately approved cold intent, predecessor failure and immutable grant."""

from datetime import UTC, datetime

from opentelemetry import trace
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import UpgradeExpiredRecoveryRow, UpgradeRecoverySuccessionRow
from coire_api.training.service import training_id
from coire_api.upgrades.cold_continuation_context import load_expired_cold_predecessor
from coire_api.upgrades.cold_continuation_relay import (
    ColdContinuationObservationRelay,
    verify_observation_context,
)
from coire_api.upgrades.cold_continuation_schema import inspect_checks
from coire_api.upgrades.expired_recovery import accept_expired_recovery
from coire_api.upgrades.expired_recovery_data import capture_data_snapshot
from coire_api.upgrades.expired_recovery_orchestration import load_context
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryIntent
from coire_core.models.upgrade_recovery_continuation import (
    UpgradeColdContinuationGrant,
    UpgradeColdContinuationRead,
    UpgradeColdContinuationRequest,
    UpgradeColdRecoverySupersession,
)
from coire_core.models.upgrades import UpgradeMaintenance
from coire_core.upgrade_recovery import capture_expired_native_recovery

tracer = trace.get_tracer("coire.api.cold_continuation_accept")


@tracer.start_as_current_span("coire.api.cold_continuation_accept", record_exception=False)
async def accept_continuation(
    session: AsyncSession,
    principal: Principal,
    body: UpgradeColdContinuationRequest,
    relay: ColdContinuationObservationRelay,
) -> tuple[UpgradeExpiredRecoveryIntent, bool]:
    """No transport I/O under locks. Caller commits both roots, grant and audits.

    A missing observation never causes a new sample or retry budget. Replay uses
    its immutable grant before reading the transient metadata cache.
    """
    actor, existing, operation, plan, support = await load_context(session, principal, body.request)
    if existing is not None:
        row = await session.scalar(
            select(UpgradeRecoverySuccessionRow).where(
                UpgradeRecoverySuccessionRow.recovery_id == existing.recovery_id
            )
        )
        if row is None or row.grant_payload.get("protocol") != "cold-source-continuation-v1":
            raise UpgradeConflict("Cold continuation retry refers to another recovery protocol")
        grant = UpgradeColdContinuationGrant.model_validate(row.grant_payload)
        if (
            grant.request_sha256 != body.sha256()
            or grant.intent != existing
            or row.grant_sha256 != grant.sha256()
            or row.predecessor_sha256 != body.previous_owner_sha256
        ):
            raise UpgradeConflict("Cold continuation retry changes immutable request or grant")
        predecessor = await session.get(
            UpgradeExpiredRecoveryRow,
            grant.unstarted_startup.intent.recovery_id
            if grant.unstarted_startup is not None
            else body.previous_recovery_id,
        )
        if (
            predecessor is None
            or predecessor.phase != "superseded"
            or predecessor.active_slot is not None
            or predecessor.resolution_payload is None
        ):
            raise UpgradeConflict("Cold continuation predecessor supersession is incomplete")
        receipt = UpgradeColdRecoverySupersession.model_validate(predecessor.resolution_payload)
        if (
            receipt.grant_sha256 != grant.sha256()
            or receipt.next_recovery_id != existing.recovery_id
            or receipt.next_intent_sha256 != existing.sha256()
        ):
            raise UpgradeConflict("Cold continuation retained predecessor link differs")
        return existing, False
    if operation is None or plan is None or support is None:
        raise UpgradeUnavailable("Cold continuation original context is incomplete")
    read = UpgradeColdContinuationRead(
        previous_intent_sha256=body.previous_intent_sha256,
        previous_owner_sha256=body.previous_owner_sha256,
    )
    predecessor, previous_intent, previous_owner = await load_expired_cold_predecessor(
        session, body.previous_recovery_id, read
    )
    if (
        plan.authority_sha256() != previous_intent.original_plan.authority_sha256()
        or not await inspect_checks(session)
    ):
        raise UpgradeConflict("Cold continuation requires the exact prepared original schema")
    proof = relay.read(body.observation_sha256)
    if (
        proof.binding.previous_recovery_id != body.previous_recovery_id
        or proof.binding.previous_owner_sha256 != body.previous_owner_sha256
        or proof.binding.previous_intent_sha256 != body.previous_intent_sha256
        or proof.previous_owner != previous_owner
        or proof.observer_release != support
    ):
        raise UpgradeConflict("Cold continuation observation differs from approved request")
    await verify_observation_context(session, proof, previous_owner.original_claim.node)
    now = datetime.now(UTC)
    if not 0 <= (now - proof.observed_at).total_seconds() <= 5:
        raise UpgradeUnavailable("Cold continuation observation expired before capture")
    intent = capture_expired_native_recovery(
        request=body.request,
        operation=operation,
        plan=plan,
        native=proof.binding.projected_native,
        support=support,
        scopes=proof.scopes,
        recovery_id=training_id(),
        accepted_by=actor,
        now=now,
    )
    if proof.binding.post_handback is not None:
        generation = int(
            await session.scalar(text("SELECT nextval('upgrade_generation_seq')")) or 0
        )
        fence = UpgradeMaintenance(
            job_id=intent.recovery_id,
            generation=generation,
            targets=(previous_owner.original_claim.node,),
            state="active",
            updated_at=intent.accepted_at,
            drain_deadline_at=intent.deadline_at,
            initiator_user_id=intent.accepted_by,
        )
        intent = UpgradeExpiredRecoveryIntent.model_validate(
            intent.model_dump(mode="json")
            | {"local_recovery_maintenance": fence.model_dump(mode="json")}
        )
    elif proof.binding.previous_local_recovery_maintenance is not None:
        intent = UpgradeExpiredRecoveryIntent.model_validate(
            intent.model_dump(mode="json")
            | {
                "inherited_local_maintenance": proof.binding.previous_local_recovery_maintenance.model_dump(
                    mode="json"
                )
            }
        )
    baseline = await capture_data_snapshot(session, intent)
    # Preserve original content, not just a newly sampled baseline after a crash.
    from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryDataSnapshot

    if predecessor.data_baseline_payload is None:
        raise UpgradeConflict("Cold continuation has no preserved Core baseline")
    before = UpgradeExpiredRecoveryDataSnapshot.model_validate(predecessor.data_baseline_payload)
    if (baseline.table_sha256s, baseline.row_counts) != (before.table_sha256s, before.row_counts):
        raise UpgradeConflict("Cold continuation cannot mask changed original user data")
    from coire_api.upgrades.cold_unstarted_lineage import unstarted_descendant

    skipped = await unstarted_descendant(session, predecessor, previous_owner)
    if skipped is not None:
        predecessor, failed_intent = skipped
        if proof.unstarted_startup is None or proof.unstarted_startup.intent != failed_intent:
            raise UpgradeConflict("Cold continuation changes unstarted custody evidence")
    elif proof.unstarted_startup is not None:
        raise UpgradeConflict("Cold continuation invents an unstarted custody link")
    grant = UpgradeColdContinuationGrant(
        grant_id=training_id(),
        request_sha256=body.sha256(),
        intent=intent,
        binding=proof.binding,
        issued_by=actor,
        issued_at=datetime.now(UTC),
        unstarted_startup=proof.unstarted_startup,
    )
    receipt = UpgradeColdRecoverySupersession(
        protocol="cold-unstarted-supersession-v1"
        if proof.unstarted_startup is not None
        else "cold-source-continuation-v1",
        unstarted_startup=proof.unstarted_startup,
        previous_recovery_id=predecessor.id,
        previous_intent_sha256=predecessor.intent_sha256,
        previous_owner_sha256=previous_owner.sha256(),
        previous_deadline_at=UpgradeExpiredRecoveryIntent.model_validate(
            predecessor.intent_payload
        ).deadline_at,
        next_recovery_id=intent.recovery_id,
        next_intent_sha256=intent.sha256(),
        grant_sha256=grant.sha256(),
        original_job_id=operation.job_id,
        original_generation=operation.generation,
        superseded_by=actor,
        superseded_at=grant.issued_at,
    )
    # Flush old separate slot inside this transaction before the unique new slot;
    # original slot and both maintenance fences remain continuously held.
    predecessor.resolution_payload = receipt.model_dump(mode="json")
    predecessor.phase, predecessor.active_slot = "superseded", None
    await session.flush()
    accepted, created = await accept_expired_recovery(
        session, principal, intent, data_baseline=baseline, native_baseline=proof.native_data
    )
    if not created or accepted != intent:
        raise UpgradeConflict("Cold continuation capture was not a distinct accepted intent")
    session.add(
        UpgradeRecoverySuccessionRow(
            id=grant.grant_id,
            recovery_id=intent.recovery_id,
            predecessor_sha256=previous_owner.sha256(),
            grant_sha256=grant.sha256(),
            grant_payload=grant.model_dump(mode="json"),
        )
    )
    await write_principal_audit(
        session,
        principal=principal,
        action="upgrades.expired_recovery.cold_continuation.accept",
        target_type="upgrade_recovery",
        target_id=intent.recovery_id,
        detail={
            "original_job_id": operation.job_id,
            "original_generation": operation.generation,
            "previous_recovery_id": previous_intent.recovery_id,
            "request_sha256": body.sha256(),
            "grant_sha256": grant.sha256(),
            "supersession": receipt.model_dump(mode="json"),
            "original_maintenance_retained": True,
        },
    )
    await session.flush()
    return intent, True
