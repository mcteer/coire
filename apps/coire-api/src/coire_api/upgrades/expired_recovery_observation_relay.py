"""Bounded ephemeral trusted snapshots, never durable recovery authority.

An API restart loses observations, not grants/claims/fences. A trusted native
producer must resample; absence/error never means pending or permits takeover.
Only an authenticated fixed-node ingress may publish. Admin orchestration must
still reauthorize and durably audit a distinct grant before any native effects.
"""

import threading
import time
from datetime import UTC, datetime, timedelta

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoverySuccessionObservation,
)


class SuccessionObservationRelay:
    """Per-API-process bounded passive data; no credentials, paths or commands."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clock_origin = datetime.now(UTC)
        self._tick_origin = time.monotonic()
        self._entries: dict[
            tuple[str, str], tuple[float, UpgradeExpiredRecoverySuccessionObservation]
        ] = {}

    def _sample_clock(self) -> tuple[datetime, float]:
        tick = time.monotonic()
        # Never make an aged physical sample young again after UTC rollback,
        # including after its cache entry was pruned. This is refusal only.
        floor = self._clock_origin + timedelta(seconds=max(0.0, tick - self._tick_origin))
        now = max(datetime.now(UTC), floor)
        # Retain forward wall-clock observations too: a later rollback must not
        # undo a freshness/deadline refusal already made by this relay process.
        self._clock_origin = now
        self._tick_origin = tick
        return now, tick

    def _prune(self, now: datetime, tick: float) -> None:
        for key, (captured, proof) in list(self._entries.items()):
            intent = proof.previous_owner.consumption.transfer.intent
            if (
                tick - captured > 5
                or not 0 <= (now - proof.observed_at).total_seconds() <= 5
                or now >= intent.deadline_at
            ):
                del self._entries[key]

    def publish(self, observation: UpgradeExpiredRecoverySuccessionObservation) -> None:
        observation = UpgradeExpiredRecoverySuccessionObservation.model_validate_json(
            observation.model_dump_json()
        )
        intent = observation.previous_owner.consumption.transfer.intent
        key = (intent.recovery_id, observation.succession_id)
        with self._lock:
            # Lock contention and canonicalization consume the physical sample's
            # window too; never acknowledge freshness sampled before exclusion.
            now, tick = self._sample_clock()
            if (
                not 0 <= (now - observation.observed_at).total_seconds() <= 5
                or now >= intent.deadline_at
            ):
                raise UpgradeUnavailable("Trusted succession sample expired or future")
            self._prune(now, tick)
            existing = self._entries.get(key)
            if existing is not None:
                prior = existing[1]
                if (
                    prior.previous_owner != observation.previous_owner
                    or prior.successor != observation.successor
                ):
                    raise UpgradeConflict("Trusted succession producer scope differs")
                if observation.observed_at < prior.observed_at:
                    raise UpgradeConflict("Trusted succession sample regressed")
                if prior == observation:
                    return  # Exact replay never refreshes monotonic retention.
            if existing is None and len(self._entries) >= 16:
                raise UpgradeUnavailable("Trusted succession snapshot capacity exhausted")
            self._entries[key] = (tick, observation)

    def read(
        self, previous: UpgradeExpiredRecoveryOwnerClaim, succession_id: str
    ) -> UpgradeExpiredRecoverySuccessionObservation:
        intent = previous.consumption.transfer.intent
        with self._lock:
            self._prune(*self._sample_clock())
            entry = self._entries.get((intent.recovery_id, succession_id))
            if entry is None:
                raise UpgradeUnavailable("Trusted succession snapshot absent or stale")
            proof = entry[1]
            if proof.previous_owner != previous:
                raise UpgradeConflict("Trusted succession predecessor differs")
            result = UpgradeExpiredRecoverySuccessionObservation.model_validate_json(
                proof.model_dump_json()
            )
            # Detaching the snapshot consumes its window too. Keep the final
            # freshness boundary inside exclusion, rather than returning stale data.
            self._prune(*self._sample_clock())
            if (intent.recovery_id, succession_id) not in self._entries:
                raise UpgradeUnavailable("Trusted succession snapshot expired during read")
            return result
