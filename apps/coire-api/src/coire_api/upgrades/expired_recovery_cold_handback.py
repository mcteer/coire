"""Audited Source handback intent, never a release from process absence."""

from datetime import UTC, datetime

from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.upgrades.expired_recovery_cold import pending_binding
from coire_api.upgrades.expired_recovery_cold_verification import verify_live_verification
from coire_api.upgrades.expired_recovery_owner import verify_intent_binding
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdHandbackIntent,
    UpgradeExpiredRecoveryColdHandbackReceipt,
)

tracer = trace.get_tracer("coire.api.cold_recovery_handback")


@tracer.start_as_current_span("coire.api.cold_recovery_handback_intent")
async def accept_handback_intent(
    session: AsyncSession, handback: UpgradeExpiredRecoveryColdHandbackIntent, node: str
) -> tuple[UpgradeExpiredRecoveryColdHandbackIntent, bool]:
    handback = UpgradeExpiredRecoveryColdHandbackIntent.model_validate_json(
        handback.model_dump_json()
    )
    proof = handback.verification
    owner = proof.request.smoke.smoke.source.launch.source.projection.owner
    if (
        await verify_live_verification(
            session, owner.intent.recovery_id, proof.sha256(), owner.process, node
        )
        != proof
    ):
        raise UpgradeConflict("Cold Source handback verification differs")
    row, _ = await pending_binding(session, owner.intent.recovery_id, node)
    if row.cold_handback_intent_payload is not None:
        existing = UpgradeExpiredRecoveryColdHandbackIntent.model_validate(
            row.cold_handback_intent_payload
        )
        if existing != handback:
            raise UpgradeConflict("Cold Source handback retry changes captured intent")
        return existing, False
    if (
        row.phase != "handback_prepared"
        or not proof.verified_at
        <= handback.started_at
        <= datetime.now(UTC)
        < owner.intent.deadline_at
    ):
        raise UpgradeUnavailable("Cold Source handback intent is not live")
    actor = await verify_intent_binding(session, row, owner.intent, node)
    row.cold_handback_intent_payload = handback.model_dump(mode="json")
    row.phase = "handing_back"
    await write_principal_audit(
        session,
        principal=actor,
        action="upgrades.expired_recovery.source.handback",
        target_type="upgrade_recovery",
        target_id=owner.intent.recovery_id,
        detail={
            "original_job_id": owner.original_claim.job_id,
            "handback_sha256": handback.sha256(),
            "original_maintenance_retained": True,
        },
    )
    await session.flush()
    return handback, True


async def verify_handback_intent(
    session: AsyncSession, handback: UpgradeExpiredRecoveryColdHandbackIntent, node: str
) -> UpgradeExpiredRecoveryColdHandbackIntent:
    owner = handback.verification.request.smoke.smoke.source.launch.source.projection.owner
    await verify_live_verification(
        session, owner.intent.recovery_id, handback.verification.sha256(), owner.process, node
    )
    row, _ = await pending_binding(session, owner.intent.recovery_id, node)
    if row.phase not in {
        "handing_back",
        "resolving",
    } or row.cold_handback_intent_payload != handback.model_dump(mode="json"):
        raise UpgradeConflict("Cold Source handback has no exact live accepted intent")
    return handback


@tracer.start_as_current_span("coire.api.cold_recovery_handback_receipt")
async def accept_handback_receipt(
    session: AsyncSession, receipt: UpgradeExpiredRecoveryColdHandbackReceipt, node: str
) -> tuple[UpgradeExpiredRecoveryColdHandbackReceipt, bool]:
    """Record Source ownership without releasing either maintenance or slot.

    The trusted node producer proves physical ownership. A retained receipt is
    immutable history; replay cannot renew its observation or release authority.
    """
    receipt = UpgradeExpiredRecoveryColdHandbackReceipt.model_validate_json(
        receipt.model_dump_json()
    )
    handback = receipt.helper.handback
    owner = handback.verification.request.smoke.smoke.source.launch.source.projection.owner
    await verify_live_verification(
        session, owner.intent.recovery_id, handback.verification.sha256(), owner.process, node
    )
    row, _ = await pending_binding(session, owner.intent.recovery_id, node)
    if row.cold_handback_intent_payload != handback.model_dump(mode="json"):
        raise UpgradeConflict("Source handback receipt has no exact accepted intent")
    if row.cold_handback_receipt_payload is not None:
        existing = UpgradeExpiredRecoveryColdHandbackReceipt.model_validate(
            row.cold_handback_receipt_payload
        )
        if existing != receipt:
            raise UpgradeConflict("Source handback receipt retry changes retained history")
        return existing, False
    now = datetime.now(UTC)
    if row.phase != "handing_back" or not 0 <= (now - receipt.observed_at).total_seconds() <= 5:
        raise UpgradeUnavailable("Source handback receipt is not fresh or live")
    actor = await verify_intent_binding(session, row, owner.intent, node)
    row.cold_handback_receipt_payload = receipt.model_dump(mode="json")
    row.phase = "resolving"
    await write_principal_audit(
        session,
        principal=actor,
        action="upgrades.expired_recovery.source.handback.verified",
        target_type="upgrade_recovery",
        target_id=owner.intent.recovery_id,
        detail={
            "original_job_id": owner.original_claim.job_id,
            "handback_receipt_sha256": receipt.sha256(),
            "original_maintenance_retained": True,
        },
    )
    await session.flush()
    return receipt, True
