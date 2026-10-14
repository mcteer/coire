"""Durable pre-create executor intent; it never starts a candidate or transfers a writer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreBootstrapSeed,
    CoreHandoverCandidateStart,
    CoreHandoverCreation,
    CoreHandoverCreationRecoveryIntent,
    CoreHandoverPreparation,
    CoreHandoverTransferIntent,
    ExecutorPassiveReadiness,
)
from coire_core.upgrade_journal import PrivateUpgradeStore


class HandoverCreationJournal:
    @classmethod
    def open_retained(
        cls, root: Path, preparation: CoreHandoverPreparation
    ) -> HandoverCreationJournal:
        """A passive reader must never manufacture the incumbent's preparation."""
        if not root.exists():
            raise UpgradeUnavailable("Retained executor preparation is absent")
        reader = cls.__new__(cls)
        reader.preparation = CoreHandoverPreparation.model_validate(
            preparation.model_dump(mode="json")
        )
        reader.store = PrivateUpgradeStore(root)
        reader.current()
        return reader

    def __init__(self, root: Path, preparation: CoreHandoverPreparation) -> None:
        self.preparation = CoreHandoverPreparation.model_validate(
            preparation.model_dump(mode="json")
        )
        self.store = PrivateUpgradeStore(root)
        with self.store.ownership("creation.lock"):
            previous = self.store.read("preparation.json", CoreHandoverPreparation)
            if previous is not None and previous != self.preparation:
                raise UpgradeConflict("Executor creation preparation differs from retained intent")
            if (
                previous is None
                and self.store.read("creation.json", CoreHandoverCreation) is not None
            ):
                raise UpgradeConflict("Executor creation lost its retained preparation")
            if previous is None:
                self.store.write("preparation.json", self.preparation, replace=False)

    def expected(self) -> CoreHandoverCreation:
        return CoreHandoverCreation(
            preparation_sha256=self.preparation.sha256(),
            candidate_name=self.preparation.candidate_name,
            candidate_image_id=self.preparation.candidate_image_id,
            generation=self.preparation.generation,
        )

    def begin_creation_recovery(
        self, *, now: datetime | None = None
    ) -> CoreHandoverCreationRecoveryIntent:
        with self.store.ownership("creation-recovery.lock"):
            previous = self.store.read(
                "creation-recovery-intent.json", CoreHandoverCreationRecoveryIntent
            )
            if previous is not None:
                if (
                    previous.preparation_sha256 != self.preparation.sha256()
                    or previous.generation != self.preparation.generation
                    or previous.detected_at < self.preparation.prepared_at
                ):
                    raise UpgradeConflict("Creation recovery belongs to another preparation")
                return previous
            at = now or datetime.now(UTC)
            if at < self.preparation.prepared_at:
                raise UpgradeConflict("Creation recovery predates its accepted preparation")
            intent = CoreHandoverCreationRecoveryIntent(
                preparation_sha256=self.preparation.sha256(),
                generation=self.preparation.generation,
                detected_at=at,
                deadline_at=at + timedelta(seconds=300),
            )
            self.store.write("creation-recovery-intent.json", intent, replace=False)
            return intent

    def current(self) -> CoreHandoverCreation | None:
        if self.store.read("preparation.json", CoreHandoverPreparation) != self.preparation:
            raise UpgradeConflict("Executor creation preparation changed or disappeared")
        value = self.store.read("creation.json", CoreHandoverCreation)
        if value is not None and value.model_dump(
            exclude={"outcome", "candidate_container_id", "observed_at"}
        ) != self.expected().model_dump(
            exclude={"outcome", "candidate_container_id", "observed_at"}
        ):
            raise UpgradeConflict("Executor creation intent differs from fixed preparation")
        return value

    def begin(self, *, now: datetime | None = None) -> CoreHandoverCreation:
        clock = now or datetime.now(UTC)
        with self.store.ownership("creation.lock"):
            retained = self.current()
            if retained is not None:
                return retained
            if not self.preparation.prepared_at <= clock < self.preparation.deadline_at:
                raise UpgradeUnavailable(
                    "Executor passive preparation deadline elapsed or is future"
                )
            value = self.expected()
            self.store.write("creation.json", value, replace=False)
            return value

    def dispatch(self, expected: CoreHandoverCreation, *, now: datetime | None = None) -> bool:
        """Fsync exactly once before create; an uncertain absence cannot renew the effect."""
        clock = now or datetime.now(UTC)
        with self.store.ownership("creation.lock"):
            current = self.current()
            if current is None or expected != self.expected():
                raise UpgradeConflict("Executor dispatch has no matching creation intent")
            if current.outcome != "intent":
                return False
            if not self.preparation.prepared_at <= clock < self.preparation.deadline_at:
                raise UpgradeUnavailable("Executor creation dispatch deadline elapsed or is future")
            self.store.write("creation.json", current.model_copy(update={"outcome": "dispatched"}))
            return True

    def observe(
        self,
        expected: CoreHandoverCreation,
        container_id: str,
        *,
        observed_at: datetime,
        now: datetime | None = None,
    ) -> CoreHandoverCreation:
        clock = now or datetime.now(UTC)
        with self.store.ownership("creation.lock"):
            current = self.current()
            if current is None or expected != self.expected():
                raise UpgradeConflict("Executor observation has no matching creation intent")
            if current.outcome == "observed":
                if current.candidate_container_id != container_id:
                    raise UpgradeConflict("Executor creation reconciled another container")
                return current
            if (
                not 0 <= (clock - observed_at).total_seconds() <= 5
                or observed_at < self.preparation.prepared_at
            ):
                raise UpgradeUnavailable("Executor creation has no fresh original observation")
            value = CoreHandoverCreation.model_validate(
                current.model_dump()
                | {
                    "outcome": "observed",
                    "candidate_container_id": container_id,
                    "observed_at": observed_at,
                }
            )
            if (
                container_id == self.preparation.incumbent.executor_container_id
                or container_id in self.preparation.incumbent.source_container_ids.values()
            ):
                raise UpgradeConflict("Executor creation cannot adopt a retained Source")
            self.store.write("creation.json", value)
            return value

    def candidate_seed(self) -> CoreBootstrapSeed:
        """Retain the observed candidate incarnation without selecting it as recovery owner."""
        with self.store.ownership("creation.lock"):
            creation = self.current()
            if (
                creation is None
                or creation.outcome != "observed"
                or creation.candidate_container_id is None
                or creation.observed_at is None
            ):
                raise UpgradeConflict("Candidate seed has no verified creation observation")
            seed = CoreBootstrapSeed(
                source=self.preparation.incumbent.source,
                executor_release=self.preparation.candidate_release,
                source_container_ids=self.preparation.incumbent.source_container_ids,
                executor_runtime_id=self.preparation.candidate_runtime_id,
                executor_container_id=creation.candidate_container_id,
                executor_image_id=self.preparation.candidate_image_id,
                generation=self.preparation.generation,
                prepared_at=creation.observed_at,
            )
            retained = self.store.read("candidate-seed.json", CoreBootstrapSeed)
            if retained is not None and retained != seed:
                raise UpgradeConflict(
                    "Candidate seed differs from its retained observed incarnation"
                )
            if retained is None:
                self.store.write("candidate-seed.json", seed, replace=False)
            return seed

    def retained_candidate_seed(self) -> CoreBootstrapSeed:
        """Verify an already published incarnation without writing or selecting it."""
        creation = self.current()
        seed = self.store.read("candidate-seed.json", CoreBootstrapSeed)
        if seed is None:
            raise UpgradeUnavailable("Passive candidate seed is absent")
        if (
            creation is None
            or creation.outcome != "observed"
            or seed.source != self.preparation.incumbent.source
            or seed.executor_source != self.preparation.candidate_release
            or seed.source_container_ids != self.preparation.incumbent.source_container_ids
            or seed.executor_runtime_id != self.preparation.candidate_runtime_id
            or seed.executor_container_id != creation.candidate_container_id
            or seed.executor_image_id != self.preparation.candidate_image_id
            or seed.generation != self.preparation.generation
            or seed.prepared_at != creation.observed_at
        ):
            raise UpgradeConflict("Passive candidate seed differs from retained creation")
        return seed

    def expected_start(self) -> CoreHandoverCandidateStart:
        seed = self.retained_candidate_seed()
        return CoreHandoverCandidateStart(
            preparation_sha256=self.preparation.sha256(),
            candidate_seed_sha256=seed.sha256(),
            candidate_container_id=seed.executor_container_id,
            generation=seed.generation,
            deadline_at=self.preparation.deadline_at,
        )

    def current_start(self) -> CoreHandoverCandidateStart | None:
        value = self.store.read("candidate-start.json", CoreHandoverCandidateStart)
        if value is not None and value.model_dump(
            exclude={"outcome", "started_at", "observed_at"}
        ) != self.expected_start().model_dump(exclude={"outcome", "started_at", "observed_at"}):
            raise UpgradeConflict("Candidate start differs from retained creation authority")
        return value

    def begin_start(self, *, now: datetime | None = None) -> CoreHandoverCandidateStart:
        clock = now or datetime.now(UTC)
        with self.store.ownership("start.lock"):
            retained = self.current_start()
            if retained is not None:
                return retained
            expected = self.expected_start()
            if not self.preparation.prepared_at <= clock < expected.deadline_at:
                raise UpgradeUnavailable("Candidate start original deadline elapsed or is future")
            self.store.write("candidate-start.json", expected, replace=False)
            return expected

    def dispatch_start(
        self, expected: CoreHandoverCandidateStart, *, now: datetime | None = None
    ) -> bool:
        clock = now or datetime.now(UTC)
        with self.store.ownership("start.lock"):
            current = self.current_start()
            if current is None or expected != self.expected_start():
                raise UpgradeConflict("Candidate start dispatch has no matching intent")
            if current.outcome != "intent":
                return False
            if not self.preparation.prepared_at <= clock < expected.deadline_at:
                raise UpgradeUnavailable("Candidate start original dispatch deadline elapsed")
            self.store.write(
                "candidate-start.json", current.model_copy(update={"outcome": "dispatched"})
            )
            return True

    def observe_start(
        self,
        *,
        started_at: datetime,
        observed_at: datetime,
        now: datetime | None = None,
    ) -> CoreHandoverCandidateStart:
        clock = now or datetime.now(UTC)
        with self.store.ownership("start.lock"):
            current = self.current_start()
            if current is None or current.outcome == "intent":
                raise UpgradeConflict("Candidate process has no dispatched start intent")
            if (
                not self.preparation.prepared_at <= started_at < self.preparation.deadline_at
                or started_at > observed_at
                or not 0 <= (clock - observed_at).total_seconds() <= 5
            ):
                raise UpgradeUnavailable("Candidate process birth or original observation differs")
            if current.outcome == "observed":
                if current.started_at != started_at:
                    raise UpgradeConflict("Candidate process restarted after its observed start")
                return current
            observed = CoreHandoverCandidateStart.model_validate(
                current.model_dump()
                | {
                    "outcome": "observed",
                    "started_at": started_at,
                    "observed_at": observed_at,
                }
            )
            self.store.write("candidate-start.json", observed)
            return observed

    def record_passive(
        self,
        proof: ExecutorPassiveReadiness,
        *,
        now: datetime | None = None,
    ) -> None:
        """Retain fresh protocol evidence; it grants neither transfer nor readiness."""
        clock = now or datetime.now(UTC)
        with self.store.ownership("protocol.lock"):
            seed = self.retained_candidate_seed()
            started = self.current_start()
            if (
                started is None
                or started.outcome != "observed"
                or proof.preparation_sha256 != self.preparation.sha256()
                or proof.candidate_seed_sha256 != seed.sha256()
                or proof.generation != seed.generation
                or proof.candidate_runtime_id != seed.executor_runtime_id
                or proof.candidate_container_id != seed.executor_container_id
                or proof.candidate_image_id != seed.executor_image_id
                or proof.incumbent_runtime_id != self.preparation.incumbent.executor_runtime_id
                or proof.incumbent_container_id != self.preparation.incumbent.executor_container_id
            ):
                raise UpgradeConflict("Passive protocol evidence differs from observed candidate")
            if (
                not self.preparation.prepared_at <= clock < self.preparation.deadline_at
                or not 0 <= (clock - proof.observed_at).total_seconds() <= 5
            ):
                raise UpgradeUnavailable(
                    "Passive protocol evidence original deadline or freshness differs"
                )
            previous = self.store.read("passive-proof.json", ExecutorPassiveReadiness)
            transfer = self.store.read("transfer-intent.json", CoreHandoverTransferIntent)
            if transfer is not None and previous != proof:
                raise UpgradeConflict("Consumed transfer freezes its original passive evidence")
            if previous is not None and previous.observed_at > proof.observed_at:
                raise UpgradeConflict("Passive protocol original observation regressed")
            if previous is not None and previous.observed_at == proof.observed_at:
                if previous != proof:
                    raise UpgradeConflict("Passive protocol repeated observation changed")
                return
            self.store.write("passive-proof.json", proof)

    def retained_transfer(self) -> CoreHandoverTransferIntent:
        seed = self.retained_candidate_seed()
        proof = self.store.read("passive-proof.json", ExecutorPassiveReadiness)
        intent = self.store.read("transfer-intent.json", CoreHandoverTransferIntent)
        if proof is None or intent is None:
            raise UpgradeUnavailable("Executor handover consumed transfer intent is absent")
        if (
            intent.preparation_sha256 != self.preparation.sha256()
            or intent.candidate_seed_sha256 != seed.sha256()
            or intent.passive_proof_sha256 != proof.sha256()
            or intent.generation != self.preparation.generation
            or not self.preparation.prepared_at
            <= intent.consumed_at
            < intent.deadline_at
            <= self.preparation.deadline_at
        ):
            raise UpgradeConflict("Consumed transfer differs from its captured handover authority")
        return intent

    def record_transfer(self, intent: CoreHandoverTransferIntent) -> None:
        """Retain the original API-consumed intent, including expired reconciliation evidence.

        This journal does not grant a kernel transfer. That boundary must verify
        the retained intent's original deadline and the exact handover phase.
        """
        with self.store.ownership("protocol.lock"):
            seed = self.retained_candidate_seed()
            proof = self.store.read("passive-proof.json", ExecutorPassiveReadiness)
            if (
                proof is None
                or intent.preparation_sha256 != self.preparation.sha256()
                or intent.candidate_seed_sha256 != seed.sha256()
                or intent.passive_proof_sha256 != proof.sha256()
                or intent.generation != self.preparation.generation
                or not self.preparation.prepared_at
                <= intent.consumed_at
                < intent.deadline_at
                <= self.preparation.deadline_at
            ):
                raise UpgradeConflict("Transfer intent differs from retained passive evidence")
            retained = self.store.read("transfer-intent.json", CoreHandoverTransferIntent)
            if retained is not None:
                if retained != intent:
                    raise UpgradeConflict("Consumed executor transfer intent cannot be replaced")
                self.retained_transfer()
                return
            self.store.write("transfer-intent.json", intent, replace=False)
