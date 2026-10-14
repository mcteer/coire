"""Close only an expired attempt that received no native effect authority.

The original operation, slot, fences and history remain intact. This endpoint
cannot accept another intent or capture another recovery budget.
"""

import hashlib
import logging
import threading
import time
from datetime import UTC, datetime, timedelta

from opentelemetry import metrics, trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import AuditRow, UpgradeExpiredRecoveryRow, UpgradeOperationRow
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.expired_recovery_data import (
    capture_unstarted_data_snapshot,
    verify_unstarted_data_preserved,
)
from coire_api.upgrades.grants import verify_fenced
from coire_api.upgrades.persistence import get_release, operation_lock
from coire_api.upgrades.recovery_support_scope import require_original
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryCancellation,
    UpgradeExpiredRecoveryColdClaim,
    UpgradeExpiredRecoveryDataSnapshot,
    UpgradeExpiredRecoveryIntent,
    UpgradeExpiredRecoveryNativeDataSnapshot,
    UpgradeExpiredRecoveryUnstartedCloseRequest,
    UpgradeExpiredRecoveryUnstartedClosure,
    UpgradeExpiredRecoveryUnstartedObservation,
)
from coire_core.models.upgrades import UpgradeExecutionPlan, UpgradeOperation

tracer = trace.get_tracer("coire.api.unstarted_recovery")
logger = logging.getLogger(__name__)
closures = metrics.get_meter("coire.api.unstarted_recovery").create_counter(
    "coire_upgrade_unstarted_recovery_closures_total"
)
ACTION = "upgrades.expired_recovery.close_unstarted"


async def unstarted_context(
    session: AsyncSession, recovery_id: str, intent_sha256: str, node: str
) -> tuple[UpgradeExpiredRecoveryRow, UpgradeExpiredRecoveryIntent]:
    await operation_lock(session)
    row = await session.get(UpgradeExpiredRecoveryRow, recovery_id, with_for_update=True)
    if row is None:
        raise UpgradeUnavailable("Unstarted recovery context is absent")
    intent = UpgradeExpiredRecoveryIntent.model_validate(row.intent_payload)
    request = intent.request
    require_original(
        request.original_job_id, request.original_generation, request.expected_source_sha256
    )
    if intent.original_plan.operation.node != node:
        raise UpgradeForbidden()
    cancelled = None
    if row.cancellation_payload is not None:
        cancelled = UpgradeExpiredRecoveryCancellation.model_validate(row.cancellation_payload)
        if (
            cancelled.recovery_id != recovery_id
            or cancelled.intent_sha256 != intent.sha256()
            or cancelled.original_job_id != request.original_job_id
            or cancelled.original_generation != request.original_generation
            or cancelled.cancelled_at < intent.accepted_at
        ):
            raise UpgradeConflict("Unstarted recovery cancellation scope differs")
    cold = None
    if row.cold_claim_payload is not None:
        try:
            cold = UpgradeExpiredRecoveryColdClaim.model_validate(row.cold_claim_payload)
        except ValueError:
            raise UpgradeConflict(
                "Recovery is not an expired attempt without issued effect authority"
            ) from None
        if cold.intent != intent or cancelled is None:
            raise UpgradeConflict("Pre-projection closure requires exact cancelled cold authority")
    if (
        row.intent_sha256 != intent.sha256()
        or intent_sha256 != row.intent_sha256
        or row.original_job_id != request.original_job_id
        or row.active_slot != 1
        or row.phase != ("recovery_required" if cancelled is not None else "accepted")
        or intent.request.recovery_mode != "cold"
        or datetime.now(UTC) < intent.deadline_at
        or any(
            value is not None
            for value in (
                row.transfer_payload,
                row.consumption_payload,
                row.owner_claim_payload,
                row.cold_smoke_payload,
                row.cold_verification_payload,
                row.cold_handback_intent_payload,
                row.cold_handback_receipt_payload,
                row.resolution_payload,
            )
        )
    ):
        raise UpgradeConflict("Recovery is not an expired attempt without issued effect authority")
    original = await session.get(UpgradeOperationRow, request.original_job_id, with_for_update=True)
    if original is None or original.execution_plan is None:
        raise UpgradeConflict("Unstarted recovery original operation is absent")
    operation = UpgradeOperation.model_validate(original.payload)
    plan = UpgradeExecutionPlan.model_validate(original.execution_plan)
    if (
        original.active_slot != 1
        or original.phase != "recovery_required"
        or operation.phase.value != "recovery_required"
        or operation.finished_at is not None
        or operation.authority_sha256() != plan.operation.authority_sha256()
        or plan.authority_sha256() != intent.original_plan.authority_sha256()
        or (await get_release(session, request.support_release_id)) != intent.support_release
    ):
        raise UpgradeConflict("Unstarted recovery original authority changed")
    await verify_fenced(session, plan)
    return row, intent


async def verify_unstarted_observation(
    session: AsyncSession, proof: UpgradeExpiredRecoveryUnstartedObservation, node: str
) -> None:
    row, intent = await unstarted_context(
        session, proof.intent.recovery_id, proof.intent.sha256(), node
    )
    if (
        proof.intent != intent
        or proof.pre_projection_owner
        != (
            UpgradeExpiredRecoveryColdClaim.model_validate(row.cold_claim_payload)
            if row.cold_claim_payload is not None
            else None
        )
        or (await get_release(session, proof.observer_release.release_id)) != proof.observer_release
    ):
        raise UpgradeConflict("Unstarted recovery observer catalog or intent differs")
    if not 0 <= (datetime.now(UTC) - proof.observed_at).total_seconds() <= 5:
        raise UpgradeUnavailable("Unstarted recovery observation is stale or future")
    if row.native_baseline_payload is None or row.data_baseline_payload is None:
        raise UpgradeConflict("Unstarted recovery has no retained data baseline")
    baseline = UpgradeExpiredRecoveryNativeDataSnapshot.model_validate(row.native_baseline_payload)
    if proof.native_data.model_copy(update={"observed_at": baseline.observed_at}) != baseline:
        raise UpgradeConflict("Unstarted recovery native data changed")
    before = UpgradeExpiredRecoveryDataSnapshot.model_validate(row.data_baseline_payload)
    verify_unstarted_data_preserved(
        before, await capture_unstarted_data_snapshot(session, intent), intent
    )


class UnstartedObservationRelay:
    """Bounded five-second metadata samples, never execution authority."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wall, self._tick = datetime.now(UTC), time.monotonic()
        self._entries: dict[str, tuple[float, UpgradeExpiredRecoveryUnstartedObservation]] = {}

    def _prune(self) -> tuple[datetime, float]:
        tick = time.monotonic()
        now = max(datetime.now(UTC), self._wall + timedelta(seconds=max(0, tick - self._tick)))
        self._wall, self._tick = now, tick
        for key, (captured, proof) in list(self._entries.items()):
            if tick - captured > 5 or not 0 <= (now - proof.observed_at).total_seconds() <= 5:
                del self._entries[key]
        return now, tick

    def publish(self, proof: UpgradeExpiredRecoveryUnstartedObservation) -> None:
        proof = UpgradeExpiredRecoveryUnstartedObservation.model_validate_json(
            proof.model_dump_json()
        )
        with self._lock:
            now, tick = self._prune()
            if not 0 <= (now - proof.observed_at).total_seconds() <= 5:
                raise UpgradeUnavailable("Unstarted recovery sample is stale or future")
            if len(self._entries) >= 16 and proof.sha256() not in self._entries:
                raise UpgradeUnavailable("Unstarted recovery observation capacity exhausted")
            self._entries[proof.sha256()] = tick, proof

    def read(self, digest: str) -> UpgradeExpiredRecoveryUnstartedObservation:
        with self._lock:
            self._prune()
            entry = self._entries.get(digest)
            if entry is None:
                raise UpgradeUnavailable("Unstarted recovery sample is absent or stale")
            return UpgradeExpiredRecoveryUnstartedObservation.model_validate_json(
                entry[1].model_dump_json()
            )


@tracer.start_as_current_span("coire.api.close_unstarted_recovery")
async def close_unstarted_recovery(
    session: AsyncSession,
    principal: Principal,
    recovery_id: str,
    request: UpgradeExpiredRecoveryUnstartedCloseRequest,
    relay: UnstartedObservationRelay,
) -> tuple[UpgradeExpiredRecoveryUnstartedClosure, bool]:
    await operation_lock(session)
    actor = await authorize(session, principal)
    previous = await session.scalar(
        select(AuditRow).where(
            AuditRow.action == ACTION,
            AuditRow.target_id == recovery_id,
            AuditRow.detail["idempotency_sha256"].astext
            == hashlib.sha256(request.idempotency_key.encode()).hexdigest(),
        )
    )
    if previous is not None:
        receipt = UpgradeExpiredRecoveryUnstartedClosure.model_validate(previous.detail["receipt"])
        require_original(
            receipt.original_job_id,
            receipt.original_generation,
            receipt.source_sha256,
        )
        row = await session.get(UpgradeExpiredRecoveryRow, recovery_id, with_for_update=True)
        intent = (
            UpgradeExpiredRecoveryIntent.model_validate(row.intent_payload)
            if row is not None
            else None
        )
        if (
            receipt.request_sha256 != request.sha256()
            or receipt.intent_sha256 != request.intent_sha256
            or receipt.observation_sha256 != request.observation_sha256
            or receipt.recovery_id != recovery_id
            or row is None
            or intent is None
            or row.active_slot is not None
            or row.phase != receipt.phase
            or row.intent_sha256 != request.intent_sha256
            or intent.sha256() != row.intent_sha256
            or (receipt.original_job_id, receipt.original_generation, receipt.source_sha256)
            != (
                intent.request.original_job_id,
                intent.request.original_generation,
                intent.request.expected_source_sha256,
            )
            or receipt.recovery_deadline_at != intent.deadline_at
            or receipt.original_deadline_at != intent.original_recovery_deadline_at
        ):
            raise UpgradeConflict("Unstarted recovery closure replay differs")
        return receipt, False
    proof = relay.read(request.observation_sha256)
    if proof.intent.recovery_id != recovery_id or proof.intent.sha256() != request.intent_sha256:
        raise UpgradeConflict("Unstarted recovery close request has another observed scope")
    node = proof.intent.original_plan.operation.node
    if node is None:
        raise UpgradeConflict("Unstarted recovery original node is absent")
    await verify_unstarted_observation(session, proof, node)
    row, _ = await unstarted_context(session, recovery_id, request.intent_sha256, node)
    receipt = UpgradeExpiredRecoveryUnstartedClosure(
        request_sha256=request.sha256(),
        intent_sha256=request.intent_sha256,
        observation_sha256=request.observation_sha256,
        recovery_id=recovery_id,
        original_job_id=proof.intent.request.original_job_id,
        original_generation=proof.intent.request.original_generation,
        source_sha256=proof.intent.request.expected_source_sha256,
        startup_sha256=proof.startup.sha256(),
        observer_release_id=proof.observer_release.release_id,
        original_deadline_at=proof.intent.original_recovery_deadline_at,
        recovery_deadline_at=proof.intent.deadline_at,
        closed_by=actor,
        closed_at=datetime.now(UTC),
        phase="superseded" if proof.pre_projection_owner is not None else "cancelled_unchanged",
        pre_projection_owner_sha256=(
            proof.pre_projection_owner.sha256() if proof.pre_projection_owner is not None else None
        ),
    )
    row.phase, row.active_slot = receipt.phase, None
    if proof.pre_projection_owner is not None:
        row.resolution_payload = {
            "protocol": "cold-pre-projection-closure-v1",
            "owner_sha256": proof.pre_projection_owner.sha256(),
            "receipt": receipt.model_dump(mode="json"),
        }
    await write_principal_audit(
        session,
        principal=principal,
        action=ACTION,
        target_type="upgrade_recovery",
        target_id=recovery_id,
        detail={
            "idempotency_sha256": hashlib.sha256(request.idempotency_key.encode()).hexdigest(),
            "original_maintenance_retained": True,
            "receipt": receipt.model_dump(mode="json"),
        },
    )
    await session.flush()
    return receipt, True


def record_unstarted_closure(
    receipt: UpgradeExpiredRecoveryUnstartedClosure, created: bool
) -> None:
    if created:
        closures.add(1, {"outcome": "closed"})
        logger.info(
            "Unstarted recovery attempt closed; original obligations retained",
            extra={
                "job_id": receipt.original_job_id,
                "recovery_id": receipt.recovery_id,
                "generation": receipt.original_generation,
                "user_id": str(receipt.closed_by),
            },
        )
