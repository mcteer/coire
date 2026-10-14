"""Candidate kernel ownership is distinct from passive readiness and application authority."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from coire_core.deploy import deployment_mutex, host_boot_identity
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    BootstrapHandover,
    CoreBootstrapPhysicalObservation,
    CoreBootstrapSeed,
    CoreHandoverCommitIntent,
    CoreHandoverIncumbentObservation,
    CoreHandoverPreparation,
    CoreHandoverSummary,
    DeploymentHandoverHold,
    DeploymentOwnership,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.handover_creation import HandoverCreationJournal
from coire_upgrade_executor.namespace import verify_executor_namespace
from coire_upgrade_executor.ownership import BootstrapAuthority, verify_bootstrap_physical


@contextmanager
def candidate_handover_ownership(
    settings: Settings,
    journal: HandoverCreationJournal,
    physical: CoreBootstrapPhysicalObservation,
    incumbent: CoreHandoverIncumbentObservation,
    *,
    now: datetime | None = None,
) -> Iterator[BootstrapAuthority]:
    """Claim once after real Source release, while retaining a physically healthy incumbent.

    It never advertises ready, selects an image, or authorizes an application claim.
    The incumbent separately observes and commits the captured stability interval.
    """
    seed, preparation = journal.retained_candidate_seed(), journal.preparation
    verify_executor_namespace(seed)
    verify_bootstrap_physical(seed, physical)
    intent = journal.retained_transfer()
    started = journal.current_start()
    if (
        not settings.upgrades_enabled
        or preparation.incumbent.source.manifest.project != settings.upgrade_project
    ):
        raise UpgradeUnavailable("Candidate handover is outside enabled project authority")
    if (
        started is None
        or started.outcome != "observed"
        or physical.executor_started_at != started.started_at
    ):
        raise UpgradeConflict("Candidate process differs from its observed start")
    root = PrivateUpgradeStore(Path(settings.upgrade_state_dir) / "bootstrap")
    store = PrivateUpgradeStore(Path(settings.upgrade_coordination_dir))
    expected = root.read("bootstrap-handover.json", BootstrapHandover)
    previous = store.read("ownership.json", DeploymentOwnership)

    def verify(clock: datetime) -> None:
        if not intent.consumed_at <= clock < intent.deadline_at:
            raise UpgradeUnavailable(
                "Candidate original transfer intent deadline elapsed or is future"
            )
        if (
            root.read("handover-preparation.json", CoreHandoverPreparation) != preparation
            or root.read("bootstrap-handover.json", BootstrapHandover) != expected
            or expected is None
            or expected.phase != "verified"
            or expected.generation != seed.generation
            or expected.deadline_at != preparation.deadline_at
            or expected.incumbent_id != preparation.incumbent.executor_runtime_id
            or expected.incumbent_container_id != preparation.incumbent.executor_container_id
            or expected.incumbent_image_id != preparation.incumbent.executor_image_id
            or expected.candidate_id != seed.executor_runtime_id
            or expected.candidate_container_id != seed.executor_container_id
            or expected.candidate_image_id != seed.executor_image_id
            or journal.retained_transfer() != intent
            or journal.retained_candidate_seed() != seed
        ):
            raise UpgradeConflict("Candidate transfer differs from retained verified handover")
        hold = store.read("handover-hold.json", DeploymentHandoverHold)
        if (
            hold is None
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
                settings.upgrade_project,
                preparation.sha256(),
                preparation.generation,
                preparation.incumbent.executor_runtime_id,
                preparation.incumbent.executor_container_id,
                preparation.candidate_runtime_id,
            )
        ):
            raise UpgradeConflict("Candidate transfer lost its exact durable host exclusion")
        if (
            incumbent.preparation_sha256 != preparation.sha256()
            or incumbent.incumbent_runtime_id != preparation.incumbent.executor_runtime_id
            or incumbent.incumbent_container_id != preparation.incumbent.executor_container_id
            or incumbent.incumbent_image_id != preparation.incumbent.executor_image_id
            or incumbent.incumbent_started_at > preparation.prepared_at
            or not 0 <= (clock - incumbent.observed_at).total_seconds() <= 5
        ):
            raise UpgradeUnavailable("Candidate has no fresh independent retained incumbent")
        if (
            previous is None
            or previous.state != "committed"
            or previous.job_id is not None
            or (
                previous.owner_kind,
                previous.owner_identity,
                previous.project,
                previous.executor_runtime_id,
                previous.executor_container_id,
                previous.source_sha256,
                previous.generation + 1,
            )
            != (
                "executor",
                settings.upgrade_executor_id,
                settings.upgrade_project,
                preparation.incumbent.executor_runtime_id,
                preparation.incumbent.executor_container_id,
                preparation.incumbent.source.manifest_sha256,
                preparation.generation,
            )
        ):
            raise UpgradeConflict(
                "Candidate writer requires the exact incumbent's completed release"
            )

    verify(now or datetime.now(UTC))
    with deployment_mutex(
        store,
        settings.upgrade_project,
        settings.upgrade_executor_id,
        handover=(preparation.sha256(), preparation.generation),
    ) as descriptor:
        verify(now or datetime.now(UTC))
        if store.read("ownership.json", DeploymentOwnership) != previous:
            raise UpgradeConflict("Candidate prior writer changed before capture")
        verify_bootstrap_physical(seed, physical)
        claim = DeploymentOwnership(
            project=settings.upgrade_project,
            owner_kind="executor",
            stage="preparing",
            owner_identity=settings.upgrade_executor_id,
            executor_runtime_id=seed.executor_runtime_id,
            executor_container_id=seed.executor_container_id,
            boot_identity=host_boot_identity(),
            generation=preparation.generation,
            source_sha256=seed.source.manifest_sha256,
        )
        store.write("ownership.json", claim)
        authority = BootstrapAuthority(store, descriptor, claim)
        authority.verify()
        yield authority
        authority.verify()
        store.write("ownership.json", claim.model_copy(update={"state": "committed"}))


@contextmanager
def committed_candidate_ownership(
    settings: Settings,
    journal: HandoverCreationJournal,
    physical: CoreBootstrapPhysicalObservation,
    summary: CoreHandoverSummary,
) -> Iterator[BootstrapAuthority]:
    """A confirmed terminal selection reboots as a new bootstrap generation.

    It cannot resume the original transfer/stability interval, and it cannot
    authorize reclaiming an application operation or the restored incumbent.
    """
    seed, prep = journal.retained_candidate_seed(), journal.preparation
    verify_executor_namespace(seed)
    verify_bootstrap_physical(seed, physical)
    root = PrivateUpgradeStore(Path(settings.upgrade_state_dir) / "bootstrap")
    store = PrivateUpgradeStore(Path(settings.upgrade_coordination_dir))
    previous = store.read("ownership.json", DeploymentOwnership)
    decision = journal.store.read("commit-decision.json", CoreHandoverCommitIntent)
    if (
        not settings.upgrades_enabled
        or seed.source.manifest.project != settings.upgrade_project
        or summary.phase != "committed"
        or decision is None
        or summary.handover_id != decision.handover_id
        or summary.project != settings.upgrade_project
        or summary.preparation_sha256 != prep.sha256()
        or summary.generation != prep.generation
        or summary.candidate_release_id != prep.candidate_release.release_id
        or summary.incumbent_image_id != prep.incumbent.executor_image_id
        or summary.candidate_image_id != prep.candidate_image_id
        or summary.prepared_at != prep.prepared_at
        or summary.deadline_at != prep.deadline_at
        or decision.preparation_sha256 != prep.sha256()
        or decision.candidate_seed_sha256 != seed.sha256()
        or decision.generation != prep.generation
        or root.read("selected-seed.json", CoreBootstrapSeed) != seed
        or root.read("handover-preparation.json", CoreHandoverPreparation) != prep
        or previous is None
        or previous.owner_kind != "executor"
        or previous.job_id is not None
        or previous.stage != "preparing"
        or previous.project != settings.upgrade_project
        or previous.owner_identity != settings.upgrade_executor_id
        or previous.executor_runtime_id != seed.executor_runtime_id
        or previous.executor_container_id != seed.executor_container_id
        or previous.source_sha256 != seed.source.manifest_sha256
        or previous.generation < seed.generation
    ):
        raise UpgradeConflict(
            "Committed executor reconciliation lacks its exact terminal selection"
        )
    current = root.read("bootstrap-handover.json", BootstrapHandover)
    if (
        current is None
        or current.phase not in {"stable", "committed"}
        or current.generation != prep.generation
        or current.candidate_id != seed.executor_runtime_id
        or current.candidate_container_id != seed.executor_container_id
        or current.candidate_image_id != seed.executor_image_id
        or current.incumbent_id != prep.incumbent.executor_runtime_id
        or current.incumbent_container_id != prep.incumbent.executor_container_id
        or current.incumbent_image_id != prep.incumbent.executor_image_id
    ):
        raise UpgradeConflict("Committed executor reconciliation changed its retained handover")
    with deployment_mutex(
        store,
        settings.upgrade_project,
        settings.upgrade_executor_id,
        reconciled_prior=previous if previous.state == "active" else None,
        handover=(prep.sha256(), prep.generation),
    ) as descriptor:
        if (
            store.read("ownership.json", DeploymentOwnership) != previous
            or root.read("selected-seed.json", CoreBootstrapSeed) != seed
            or root.read("bootstrap-handover.json", BootstrapHandover) != current
            or journal.store.read("commit-decision.json", CoreHandoverCommitIntent) != decision
        ):
            raise UpgradeConflict("Committed executor reconciliation changed before kernel capture")
        verify_bootstrap_physical(seed, physical)
        claim = DeploymentOwnership(
            project=settings.upgrade_project,
            owner_kind="executor",
            stage="preparing",
            owner_identity=settings.upgrade_executor_id,
            executor_runtime_id=seed.executor_runtime_id,
            executor_container_id=seed.executor_container_id,
            boot_identity=host_boot_identity(),
            generation=previous.generation + 1,
            source_sha256=seed.source.manifest_sha256,
        )
        store.write("ownership.json", claim)
        authority = BootstrapAuthority(store, descriptor, claim)
        authority.verify()
        yield authority
        authority.verify()
        store.write("ownership.json", claim.model_copy(update={"state": "committed"}))
