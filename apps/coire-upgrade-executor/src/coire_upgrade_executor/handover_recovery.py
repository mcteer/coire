"""Independent retained incumbent can stop only its exact prepared candidate."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from opentelemetry import trace

from coire_core.deploy import deployment_mutex, host_boot_identity
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    BootstrapHandover,
    CoreHandoverPreparation,
    CoreHandoverRecoveryIntent,
    CoreHandoverStoppedObservation,
    CoreHandoverSummary,
    DeploymentHandoverHold,
    DeploymentOwnership,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.docker import DockerRead
from coire_upgrade_executor.handover import HandoverObserver
from coire_upgrade_executor.handover_recovery_journal import HandoverRecoveryJournal
from coire_upgrade_executor.namespace import verify_executor_namespace
from coire_upgrade_executor.ownership import BootstrapAuthority, verify_bootstrap_physical
from coire_upgrade_executor.source_observer import InitialBootstrapObserver

tracer = trace.get_tracer("coire.upgrade_executor.handover_recovery")


class RecoveryObserver(HandoverObserver):
    """Keep the original verified container response, including its physical stop state."""

    verified_reads: dict[str, DockerRead]
    preparation: CoreHandoverPreparation

    async def read(self, method: str, path: str) -> DockerRead:
        body = await super().read(method, path)
        self.verified_reads[path] = body
        return body

    async def verify_container(
        self, container_id: str, *, require_healthy: bool = True, require_running: bool = True
    ) -> tuple[datetime, datetime]:
        times = await super().verify_container(
            container_id, require_healthy=require_healthy, require_running=require_running
        )
        if container_id == self.candidate:
            body = self.verified_reads[f"/containers/{container_id}/json"]
            config = body.get("Config")
            labels = config.get("Labels") if isinstance(config, dict) else None
            expected = {
                "coire.bootstrap.preparation_sha256": self.preparation.sha256(),
                "coire.bootstrap.generation": str(self.preparation.generation),
                "coire.bootstrap.runtime_id": self.preparation.candidate_runtime_id,
            }
            if (
                body.get("Name") != "/" + self.preparation.candidate_name
                or not isinstance(labels, dict)
                or any(labels.get(key) != value for key, value in expected.items())
            ):
                raise UpgradeConflict(
                    "Executor recovery candidate preparation labels or name differ"
                )
        return times


class HandoverRecovery:
    def __init__(
        self,
        settings: Settings,
        journal: HandoverRecoveryJournal,
        observer: RecoveryObserver,
        bootstrap: PrivateUpgradeStore,
        coordination: PrivateUpgradeStore,
    ) -> None:
        self.settings, self.journal, self.observer = settings, journal, observer
        self.bootstrap, self.coordination = bootstrap, coordination
        observer.verified_reads = {}
        observer.preparation = journal.creation.preparation
        verify_executor_namespace(journal.creation.preparation.incumbent)
        self.verify_retained()

    def verify_retained(self) -> None:
        prep = self.journal.creation.preparation
        seed = self.journal.creation.retained_candidate_seed()
        value = self.bootstrap.read("bootstrap-handover.json", BootstrapHandover)
        hold = self.coordination.read("handover-hold.json", DeploymentHandoverHold)
        owner = self.coordination.read("ownership.json", DeploymentOwnership)
        if (
            not self.settings.upgrades_enabled
            or self.settings.upgrade_project != prep.incumbent.source.manifest.project
            or self.bootstrap.read("handover-preparation.json", CoreHandoverPreparation) != prep
            or value is None
            or value.phase not in {"restoring", "restored"}
            or (
                value.incumbent_id,
                value.incumbent_container_id,
                value.incumbent_image_id,
                value.candidate_id,
                value.candidate_container_id,
                value.candidate_image_id,
                value.generation,
            )
            != (
                prep.incumbent.executor_runtime_id,
                prep.incumbent.executor_container_id,
                prep.incumbent.executor_image_id,
                seed.executor_runtime_id,
                seed.executor_container_id,
                seed.executor_image_id,
                seed.generation,
            )
            or hold is None
            or hold.released
            or (
                hold.project,
                hold.preparation_sha256,
                hold.generation,
                hold.incumbent_runtime_id,
                hold.incumbent_container_id,
                hold.candidate_runtime_id,
            )
            != (
                self.settings.upgrade_project,
                prep.sha256(),
                prep.generation,
                prep.incumbent.executor_runtime_id,
                prep.incumbent.executor_container_id,
                prep.candidate_runtime_id,
            )
            or owner is None
            or owner.owner_kind != "executor"
            or owner.project != self.settings.upgrade_project
            or owner.owner_identity != self.settings.upgrade_executor_id
            or owner.job_id is not None
            or owner.source_sha256 != prep.incumbent.source.manifest_sha256
            or not (
                (
                    (owner.executor_runtime_id, owner.executor_container_id)
                    == (prep.incumbent.executor_runtime_id, prep.incumbent.executor_container_id)
                    and owner.generation >= prep.generation - 1
                )
                or (owner.executor_runtime_id, owner.executor_container_id, owner.generation)
                == (seed.executor_runtime_id, seed.executor_container_id, seed.generation)
            )
        ):
            raise UpgradeConflict("Independent executor recovery lost its exact retained authority")
        observed = self.observer.handover
        identity = (
            "incumbent_id",
            "candidate_id",
            "incumbent_container_id",
            "candidate_container_id",
            "incumbent_image_id",
            "candidate_image_id",
            "generation",
        )
        if any(getattr(observed, field) != getattr(value, field) for field in identity):
            raise UpgradeConflict("Independent executor recovery observer belongs to another epoch")
        if value.phase == "restored":
            from coire_upgrade_executor.handover_restoration import verify_restoration_ack

            ack = self.journal.store.read("restoration-ack.json", CoreHandoverSummary)
            if ack is None:
                raise UpgradeConflict("Restored executor phase has no retained API acknowledgment")
            verify_restoration_ack(self, ack)

    async def observe_candidate(self) -> DockerRead:
        self.verify_retained()
        await self.observer.observe_incumbent(self.journal.creation.preparation)
        identity = self.journal.creation.retained_candidate_seed().executor_container_id
        await self.observer.verify_container(identity, require_healthy=False, require_running=False)
        body = self.observer.verified_reads[f"/containers/{identity}/json"]
        self.verify_retained()
        if not 0 <= (datetime.now(UTC) - body.started_at).total_seconds() <= 5:
            raise UpgradeUnavailable("Executor recovery candidate physical observation expired")
        return body

    async def observe_stopped(self) -> CoreHandoverStoppedObservation:
        body = await self.observe_candidate()
        state = body.get("State")
        if not isinstance(state, dict) or state.get("Running") is not False:
            raise UpgradeUnavailable("Executor candidate has no positive current stopped state")
        seed = self.journal.creation.retained_candidate_seed()
        value = CoreHandoverStoppedObservation(
            preparation_sha256=self.journal.creation.preparation.sha256(),
            candidate_seed_sha256=seed.sha256(),
            candidate_container_id=seed.executor_container_id,
            generation=seed.generation,
            observed_at=body.started_at,
        )
        self.journal.store.write("candidate-stopped-observation.json", value)
        return value

    @tracer.start_as_current_span(
        "coire.upgrade_executor.handover_recovery_stop",
        record_exception=False,
        set_status_on_exception=False,
    )
    async def stop_candidate(self) -> CoreHandoverRecoveryIntent:
        """Positive stopped state is necessary; a timeout or 404 never proves cleanup."""
        import httpx

        # A distinct lock avoids recursively taking the journal's transition lock.
        with self.journal.store.ownership("recovery-dispatch.lock"):
            self.verify_retained()
            intent = self.journal.begin()
            body = await self.observe_candidate()
            state = body.get("State")
            if not isinstance(state, dict) or type(state.get("Running")) is not bool:
                raise UpgradeConflict("Executor recovery stop state is malformed")
            if state["Running"] is True:
                if intent.outcome == "stopped" or not self.journal.dispatch():
                    raise UpgradeUnavailable(
                        "Executor candidate stop remains uncertain; no redispatch"
                    )
                remaining = (intent.deadline_at - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    raise UpgradeUnavailable("Original executor recovery stop deadline elapsed")
                self.verify_retained()
                try:
                    async with (
                        asyncio.timeout(min(10, remaining)),
                        self.observer.client.stream(
                            "POST",
                            self.observer.proxy.copy_with(
                                path=f"/v1.46/containers/{intent.candidate_container_id}/stop",
                                query=b"t=5",
                            ),
                            timeout=min(10, remaining),
                        ) as response,
                    ):
                        if response.status_code not in {204, 304}:
                            raise UpgradeUnavailable("Executor candidate stop outcome is uncertain")
                except (httpx.HTTPError, TimeoutError):
                    raise UpgradeUnavailable(
                        "Executor candidate stop response is uncertain"
                    ) from None
                body = await self.observe_candidate()
                state = body.get("State")
            if not isinstance(state, dict) or state.get("Running") is not False:
                raise UpgradeUnavailable("Executor candidate has no positive stopped observation")
            return self.journal.observe_stopped(
                intent.candidate_container_id, observed_at=body.started_at
            )

    @asynccontextmanager
    async def incumbent_ownership(
        self, source_observer: InitialBootstrapObserver
    ) -> AsyncIterator[BootstrapAuthority]:
        """Reclaim a new generation only after positive stop and a genuinely free kernel.

        This context retains maintenance. It does not change selected images, close
        the SQL handover, or advertise ready; those require acknowledged restoration.
        """
        prep = self.journal.creation.preparation
        seed = prep.incumbent
        self.verify_retained()
        intent = self.journal.current()
        if intent is None or intent.outcome != "stopped":
            raise UpgradeConflict("Incumbent reclamation has no positive candidate stop record")
        previous = self.coordination.read("ownership.json", DeploymentOwnership)
        assert previous is not None  # verify_retained requires an exact retained owner
        with deployment_mutex(
            self.coordination,
            self.settings.upgrade_project,
            self.settings.upgrade_executor_id,
            reconciled_prior=previous if previous.state == "active" else None,
            handover=(prep.sha256(), prep.generation),
        ) as descriptor:
            self.verify_retained()
            if self.coordination.read("ownership.json", DeploymentOwnership) != previous:
                raise UpgradeConflict("Incumbent prior writer changed during reclamation")
            body = await self.observe_candidate()
            state = body.get("State")
            if not isinstance(state, dict) or state.get("Running") is not False:
                raise UpgradeUnavailable("Candidate restarted before incumbent reclamation")
            physical = await source_observer.observe()
            verify_bootstrap_physical(seed, physical)
            self.verify_retained()
            if self.journal.current() != intent:
                raise UpgradeConflict(
                    "Incumbent recovery intent changed during physical verification"
                )
            if not intent.detected_at <= datetime.now(UTC) < intent.deadline_at:
                raise UpgradeUnavailable("Original incumbent recovery deadline elapsed")
            if not 0 <= (datetime.now(UTC) - body.started_at).total_seconds() <= 5:
                raise UpgradeUnavailable("Candidate stop observation expired before reclamation")
            claim = DeploymentOwnership(
                project=self.settings.upgrade_project,
                owner_kind="executor",
                stage="preparing",
                owner_identity=self.settings.upgrade_executor_id,
                executor_runtime_id=seed.executor_runtime_id,
                executor_container_id=seed.executor_container_id,
                boot_identity=host_boot_identity(),
                generation=max(previous.generation, prep.generation) + 1,
                source_sha256=seed.source.manifest_sha256,
            )
            self.coordination.write("ownership.json", claim)
            authority = BootstrapAuthority(self.coordination, descriptor, claim)
            authority.verify()
            yield authority
            authority.verify()
            self.coordination.write(
                "ownership.json", claim.model_copy(update={"state": "committed"})
            )
