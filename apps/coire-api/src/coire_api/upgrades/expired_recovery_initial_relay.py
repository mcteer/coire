"""Authenticated transient initial observations; never recovery authority."""

import threading
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import UpgradeOperationRow
from coire_api.upgrades.persistence import get_release, operation_lock
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryObservation,
    UpgradeExpiredRecoveryRequest,
)
from coire_core.models.upgrades import UpgradeExecutionPlan, UpgradeOperation, UpgradeRelease


async def verify_observation_context(
    session: AsyncSession, proof: UpgradeExpiredRecoveryObservation, node: str
) -> None:
    """Fixed authenticated node publication is metadata, not an admin mutation."""
    from coire_api.upgrades.recovery_support_scope import require_original

    require_original(
        proof.request.original_job_id,
        proof.request.original_generation,
        proof.request.expected_source_sha256,
    )
    await operation_lock(session)
    if proof.plan.operation.node != node:
        raise UpgradeForbidden()
    row = await session.get(
        UpgradeOperationRow, proof.request.original_job_id, with_for_update=True
    )
    if row is None or row.execution_plan is None:
        raise UpgradeUnavailable("Retained recovery observation context is absent")
    operation = UpgradeOperation.model_validate(row.payload)
    plan = UpgradeExecutionPlan.model_validate(row.execution_plan)
    if (
        row.active_slot != 1
        or row.phase != "recovery_required"
        or operation.phase.value != "recovery_required"
        or operation.finished_at is not None
        or operation.authority_sha256() != plan.operation.authority_sha256()
        or plan.authority_sha256() != proof.plan.authority_sha256()
        or plan.source.manifest_sha256 != proof.request.expected_source_sha256
        or (await get_release(session, proof.request.support_release_id)).sha256()
        != proof.support.sha256()
    ):
        raise UpgradeConflict("Retained recovery observation context differs")


class InitialRecoveryObservationRelay:
    """Five-second, sixteen-entry process-local cache; restart loses only samples."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wall = datetime.now(UTC)
        self._tick = time.monotonic()
        self._entries: dict[str, tuple[float, UpgradeExpiredRecoveryObservation]] = {}

    def _clock(self) -> tuple[datetime, float]:
        tick = time.monotonic()
        now = max(datetime.now(UTC), self._wall + timedelta(seconds=max(0.0, tick - self._tick)))
        self._wall, self._tick = now, tick
        return now, tick

    def _prune(self, now: datetime, tick: float) -> None:
        for key, (captured, proof) in list(self._entries.items()):
            if tick - captured > 5 or not 0 <= (now - proof.observed_at).total_seconds() <= 5:
                del self._entries[key]

    def publish(self, proof: UpgradeExpiredRecoveryObservation) -> None:
        proof = UpgradeExpiredRecoveryObservation.model_validate_json(proof.model_dump_json())
        key = proof.request.sha256()
        with self._lock:
            now, tick = self._clock()
            if not 0 <= (now - proof.observed_at).total_seconds() <= 5:
                raise UpgradeUnavailable("Initial recovery sample is stale or future")
            self._prune(now, tick)
            existing = self._entries.get(key)
            if existing is not None:
                prior = existing[1]
                if proof.model_copy(update={"observed_at": prior.observed_at}) != prior:
                    raise UpgradeConflict("Initial recovery observation preimages changed")
                if proof.observed_at < prior.observed_at:
                    raise UpgradeConflict("Initial recovery observation regressed")
                if proof == prior:
                    return
            elif len(self._entries) >= 16:
                raise UpgradeUnavailable("Initial recovery observation capacity exhausted")
            self._entries[key] = (tick, proof)

    def read(
        self, request: UpgradeExpiredRecoveryRequest, support: UpgradeRelease
    ) -> UpgradeExpiredRecoveryObservation:
        key = request.sha256()
        with self._lock:
            self._prune(*self._clock())
            entry = self._entries.get(key)
            if entry is None:
                raise UpgradeUnavailable("Initial recovery observation absent or stale")
            proof = entry[1]
            if proof.request != request or proof.support != support:
                raise UpgradeConflict("Initial recovery observation binding differs")
            result = UpgradeExpiredRecoveryObservation.model_validate_json(proof.model_dump_json())
            self._prune(*self._clock())
            if key not in self._entries:
                raise UpgradeUnavailable("Initial recovery sample expired during read")
            return result
