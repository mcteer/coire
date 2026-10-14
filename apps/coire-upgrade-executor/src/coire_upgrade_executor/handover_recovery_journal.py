"""Fsync fixed candidate-stop intent without granting generic Docker or writer authority."""

from datetime import UTC, datetime, timedelta

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import CoreHandoverRecoveryIntent
from coire_upgrade_executor.handover_creation import HandoverCreationJournal


class HandoverRecoveryJournal:
    def __init__(self, creation: HandoverCreationJournal) -> None:
        self.creation = creation
        self.store = creation.store

    def current(self) -> CoreHandoverRecoveryIntent | None:
        seed = self.creation.retained_candidate_seed()
        value = self.store.read("recovery-intent.json", CoreHandoverRecoveryIntent)
        if value is not None and (
            value.preparation_sha256,
            value.candidate_seed_sha256,
            value.generation,
            value.incumbent_container_id,
            value.candidate_container_id,
        ) != (
            self.creation.preparation.sha256(),
            seed.sha256(),
            seed.generation,
            self.creation.preparation.incumbent.executor_container_id,
            seed.executor_container_id,
        ):
            raise UpgradeConflict("Executor recovery intent differs from its retained incarnation")
        return value

    def begin(self, *, now: datetime | None = None) -> CoreHandoverRecoveryIntent:
        clock = now or datetime.now(UTC)
        with self.store.ownership("recovery.lock"):
            current = self.current()
            if current is not None:
                return current
            if clock < self.creation.preparation.prepared_at:
                raise UpgradeUnavailable("Executor recovery failure detection is in the future")
            seed = self.creation.retained_candidate_seed()
            value = CoreHandoverRecoveryIntent(
                preparation_sha256=self.creation.preparation.sha256(),
                candidate_seed_sha256=seed.sha256(),
                generation=seed.generation,
                incumbent_container_id=self.creation.preparation.incumbent.executor_container_id,
                candidate_container_id=seed.executor_container_id,
                detected_at=clock,
                deadline_at=clock + timedelta(seconds=300),
            )
            self.store.write("recovery-intent.json", value, replace=False)
            return value

    def dispatch(self, *, now: datetime | None = None) -> bool:
        clock = now or datetime.now(UTC)
        with self.store.ownership("recovery.lock"):
            current = self.current()
            if current is None:
                raise UpgradeConflict("Executor stop has no retained recovery intent")
            if current.outcome != "intent":
                return False
            if not current.detected_at <= clock < current.deadline_at:
                raise UpgradeUnavailable("Original executor recovery stop deadline elapsed")
            self.store.write(
                "recovery-intent.json", current.model_copy(update={"outcome": "dispatched"})
            )
            return True

    def observe_stopped(
        self, candidate_container_id: str, *, observed_at: datetime, now: datetime | None = None
    ) -> CoreHandoverRecoveryIntent:
        """Retain late positive reality for reconciliation without renewing effect permission."""
        clock = now or datetime.now(UTC)
        with self.store.ownership("recovery.lock"):
            current = self.current()
            if current is None or current.candidate_container_id != candidate_container_id:
                raise UpgradeConflict("Executor stop observation belongs to another candidate")
            if current.outcome == "stopped":
                return current
            if (
                observed_at < current.detected_at
                or not 0 <= (clock - observed_at).total_seconds() <= 5
            ):
                raise UpgradeUnavailable("Executor stop has no fresh original physical observation")
            value = CoreHandoverRecoveryIntent.model_validate(
                current.model_dump() | {"outcome": "stopped", "stopped_observed_at": observed_at}
            )
            self.store.write("recovery-intent.json", value)
            return value
