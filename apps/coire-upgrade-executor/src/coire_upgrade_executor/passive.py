"""Candidate protocol attestation carries no writer or application mutation authority."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx
from opentelemetry import metrics, trace

from coire_core.deploy import idle_executor_owner
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    DeploymentOwnership,
    ExecutorPassiveReadiness,
    UpgradeReleaseRegister,
)
from coire_core.settings import Settings
from coire_core.upgrade_artifacts import verify_prepared_release
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.handover_creation import HandoverCreationJournal
from coire_upgrade_executor.namespace import verify_executor_namespace
from coire_upgrade_executor.ownership import verify_bootstrap_physical
from coire_upgrade_executor.source_observer import InitialBootstrapObserver

tracer = trace.get_tracer("coire.upgrade_executor.passive")
observations = metrics.get_meter("coire.upgrade_executor").create_counter(
    "coire_upgrade_executor_passive_observations_total", unit=""
)


class CandidatePassiveObserver:
    def __init__(
        self, settings: Settings, journal: HandoverCreationJournal, client: httpx.AsyncClient
    ) -> None:
        self.settings, self.journal, self.client = settings, journal, client
        self.preparation = journal.preparation
        self.seed = journal.retained_candidate_seed()
        self.creation = journal.current()
        if (
            not settings.upgrades_enabled
            or self.preparation.incumbent.source.manifest.project != settings.upgrade_project
            or client.follow_redirects
            or client._trust_env
        ):
            raise UpgradeUnavailable("Passive executor is outside its prepared project")
        verify_executor_namespace(self.seed)
        coordination_root = Path(settings.upgrade_coordination_dir)
        if not coordination_root.exists():
            raise UpgradeUnavailable("Passive executor shared ownership is absent")
        self.coordination = PrivateUpgradeStore(coordination_root)

    def incumbent(self) -> DeploymentOwnership:
        owner = idle_executor_owner(
            self.coordination, self.settings.upgrade_project, self.settings.upgrade_executor_id
        )
        seed = self.preparation.incumbent
        if owner is None or (
            owner.executor_runtime_id,
            owner.executor_container_id,
            owner.source_sha256,
            owner.generation + 1,
        ) != (
            seed.executor_runtime_id,
            seed.executor_container_id,
            seed.source.manifest_sha256,
            self.preparation.generation,
        ):
            raise UpgradeConflict("Passive candidate has no exact retained incumbent writer")
        return owner

    @tracer.start_as_current_span(
        "coire.upgrade_executor.passive_observe",
        record_exception=False,
        set_status_on_exception=False,
    )
    async def observe(self) -> ExecutorPassiveReadiness:
        now = datetime.now(UTC)
        if not self.preparation.prepared_at <= now < self.preparation.deadline_at:
            raise UpgradeUnavailable(
                "Passive executor original preparation deadline elapsed or is future"
            )
        if (
            self.journal.current() != self.creation
            or self.journal.retained_candidate_seed() != self.seed
        ):
            raise UpgradeConflict("Passive executor creation authority changed")
        owner = self.incumbent()
        for release in (self.seed.source, self.seed.executor_source):
            verified = await asyncio.to_thread(
                verify_prepared_release,
                Path(self.settings.upgrade_prepared_dir),
                UpgradeReleaseRegister(
                    prepared_artifact_id=release.prepared_artifact_id,
                    expected_sha256=release.manifest_sha256,
                ),
            )
            if verified != release:
                raise UpgradeConflict("Passive executor sealed provenance differs")
        physical = await InitialBootstrapObserver(
            self.settings, Path(self.settings.upgrade_prepared_dir), self.seed, self.client
        ).observe()
        verify_bootstrap_physical(self.seed, physical)
        if self.incumbent() != owner or self.journal.current() != self.creation:
            raise UpgradeConflict("Passive incumbent or candidate changed during observation")
        if datetime.now(UTC) >= self.preparation.deadline_at:
            raise UpgradeUnavailable("Passive executor observation exhausted its original deadline")
        observations.add(1, {"outcome": "verified"})
        return ExecutorPassiveReadiness(
            preparation_sha256=self.preparation.sha256(),
            candidate_seed_sha256=self.seed.sha256(),
            generation=self.preparation.generation,
            candidate_runtime_id=self.seed.executor_runtime_id,
            candidate_container_id=self.seed.executor_container_id,
            candidate_image_id=self.seed.executor_image_id,
            incumbent_runtime_id=self.preparation.incumbent.executor_runtime_id,
            incumbent_container_id=self.preparation.incumbent.executor_container_id,
            observed_at=physical.observed_at,
        )
