"""Audited verification using trusted fresh inventory outside database transactions."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coire_api.audit import write_principal_audit
from coire_api.db import UpgradeExpiredRecoveryRow
from coire_api.upgrades.expired_recovery_cold import pending_binding, verify_cold_owner
from coire_api.upgrades.expired_recovery_data import capture_data_snapshot, verify_data_preserved
from coire_api.upgrades.expired_recovery_owner import verify_intent_binding
from coire_api.upgrades.service import smoke_asset
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdClaim,
    UpgradeExpiredRecoveryColdVerification,
    UpgradeExpiredRecoveryColdVerificationRequest,
    UpgradeExpiredRecoveryDataSnapshot,
    UpgradeExpiredRecoveryNativeDataSnapshot,
)
from coire_core.models.upgrades import NodeAgentProcessIdentity, NodeUpgradeInventory
from coire_core.upgrade_recovery import verify_native_data_preserved

tracer = trace.get_tracer("coire.api.cold_recovery_verification")
InventoryObserver = Callable[
    [], Awaitable[tuple[NodeUpgradeInventory, NodeUpgradeInventory | None]]
]


async def verify_live_verification(
    session: AsyncSession,
    recovery_id: str,
    digest: str,
    consumer: NodeAgentProcessIdentity,
    node: str,
) -> UpgradeExpiredRecoveryColdVerification:
    row, intent = await pending_binding(session, recovery_id, node)
    if row.cold_verification_payload is None:
        raise UpgradeUnavailable("Cold handback has no committed Source verification")
    proof = UpgradeExpiredRecoveryColdVerification.model_validate(row.cold_verification_payload)
    owner = proof.request.smoke.smoke.source.launch.source.projection.owner
    if owner.intent != intent or owner.process != consumer or proof.sha256() != digest:
        raise UpgradeForbidden()
    if (
        row.active_slot != 1
        or row.phase not in {"handback_prepared", "handing_back", "resolving"}
        or not proof.verified_at <= datetime.now(UTC) < intent.deadline_at
    ):
        raise UpgradeUnavailable("Cold handback captured authority is exhausted or revoked")
    return proof


async def context(
    session: AsyncSession,
    body: UpgradeExpiredRecoveryColdVerificationRequest,
    node: str,
) -> tuple[UpgradeExpiredRecoveryRow, UpgradeExpiredRecoveryColdClaim]:
    owner = body.smoke.smoke.source.launch.source.projection.owner
    row, _ = await pending_binding(session, owner.intent.recovery_id, node)
    if row.cold_claim_payload != owner.model_dump(
        mode="json"
    ) or row.cold_smoke_payload != body.smoke.smoke.model_dump(mode="json"):
        raise UpgradeConflict("Cold verification has no exact accepted owner and smoke authority")
    if row.data_baseline_payload is None or row.native_baseline_payload is None:
        raise UpgradeUnavailable(
            "Cold verification has no retained Core/native preservation baseline"
        )
    return row, owner


@tracer.start_as_current_span("coire.api.cold_recovery_verify_source")
async def prepare_verification(
    sessions: async_sessionmaker[AsyncSession],
    node: str,
    body: UpgradeExpiredRecoveryColdVerificationRequest,
    observe: InventoryObserver,
) -> tuple[UpgradeExpiredRecoveryColdVerification, bool]:
    body = UpgradeExpiredRecoveryColdVerificationRequest.model_validate_json(body.model_dump_json())
    async with sessions.begin() as session:
        row, owner = await context(session, body, node)
        if row.cold_verification_payload is not None:
            existing = UpgradeExpiredRecoveryColdVerification.model_validate(
                row.cold_verification_payload
            )
            if existing.request != body:
                raise UpgradeConflict("Cold verification retry changes retained qualification")
            await verify_live_verification(
                session, owner.intent.recovery_id, existing.sha256(), owner.process, node
            )
            return existing, False
        await verify_cold_owner(
            session, owner.intent.recovery_id, owner.sha256(), owner.process, node
        )
    # No database snapshot or operation row lock is held over authenticated I/O.
    try:
        async with asyncio.timeout(
            min(5.0, max(0.001, (owner.intent.deadline_at - datetime.now(UTC)).total_seconds()))
        ):
            local, peer = await observe()
    except TimeoutError:
        raise UpgradeUnavailable(
            "Cold verification inventory observation budget exhausted"
        ) from None
    async with sessions.begin() as session:
        row, current = await context(session, body, node)
        if current != owner:
            raise UpgradeConflict("Cold verification owner changed during inventory observation")
        if row.cold_verification_payload is not None:
            existing = UpgradeExpiredRecoveryColdVerification.model_validate(
                row.cold_verification_payload
            )
            if existing.request != body:
                raise UpgradeConflict("Concurrent cold verification changes qualification")
            await verify_live_verification(
                session, owner.intent.recovery_id, existing.sha256(), owner.process, node
            )
            return existing, False
        await verify_cold_owner(
            session, owner.intent.recovery_id, owner.sha256(), owner.process, node
        )
        plan = owner.intent.original_plan
        if await smoke_asset(session, plan.model_id, (node,)) != (
            plan.model_id,
            plan.model_variant_id,
            plan.model_slug,
            plan.model_manifest_sha256,
        ):
            raise UpgradeConflict("Cold verification registry asset changed")
        before = UpgradeExpiredRecoveryDataSnapshot.model_validate(row.data_baseline_payload)
        native_before = UpgradeExpiredRecoveryNativeDataSnapshot.model_validate(
            row.native_baseline_payload
        )
        after = await capture_data_snapshot(session, owner.intent)
        verify_data_preserved(before, after, owner.intent)
        verify_native_data_preserved(native_before, body.native_after, owner.intent)
        proof = UpgradeExpiredRecoveryColdVerification(
            request=body,
            data_before=before,
            data_after=after,
            native_before=native_before,
            local=local,
            peer=peer,
            verified_at=datetime.now(UTC),
        )
        actor = await verify_intent_binding(session, row, owner.intent, node)
        row.cold_verification_payload = proof.model_dump(mode="json")
        row.phase = "handback_prepared"
        await write_principal_audit(
            session,
            principal=actor,
            action="upgrades.expired_recovery.source.verify",
            target_type="upgrade_recovery",
            target_id=owner.intent.recovery_id,
            detail={
                "original_job_id": owner.original_claim.job_id,
                "verification_sha256": proof.sha256(),
                "source_sha256": owner.intent.request.expected_source_sha256,
            },
        )
        await session.flush()
        return proof, True
