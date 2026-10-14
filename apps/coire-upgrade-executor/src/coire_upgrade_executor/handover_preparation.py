"""Incumbent-only passive creation from retained accepted API authority."""

from pathlib import Path

import httpx
from opentelemetry import trace

from coire_core.errors import CoireError, UpgradeConflict, UpgradeForbidden
from coire_core.models.upgrades import (
    BootstrapHandover,
    CoreBootstrapPhysicalObservation,
    CoreBootstrapSeed,
    CoreHandoverPreparation,
    CoreHandoverSummary,
)
from coire_core.settings import Settings
from coire_core.upgrade_handover_unchanged import verify_unchanged_files
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.bootstrap import BootstrapJournal
from coire_upgrade_executor.handover_creation import HandoverCreationJournal
from coire_upgrade_executor.handover_docker import HandoverDocker
from coire_upgrade_executor.ownership import BootstrapAuthority, verify_bootstrap_physical

tracer = trace.get_tracer("coire.upgrade_executor.handover")


def retain_preparation(
    settings: Settings, preparation: CoreHandoverPreparation
) -> tuple[PrivateUpgradeStore, HandoverCreationJournal]:
    root = PrivateUpgradeStore(Path(settings.upgrade_state_dir) / "bootstrap")
    with root.ownership("handover-publication.lock"):
        retained = root.read("handover-preparation.json", CoreHandoverPreparation)
        if retained is not None and retained != preparation:
            archive_completed_handover(root, retained, preparation)
        journal = HandoverCreationJournal(
            root.root / "handover" / str(preparation.generation), preparation
        )
        if retained != preparation:
            root.write("handover-preparation.json", preparation, replace=retained is not None)
    return root, journal


def retain_unchanged_preparation(
    settings: Settings,
    preparation: CoreHandoverPreparation,
    authority: BootstrapAuthority,
    physical: CoreBootstrapPhysicalObservation,
) -> HandoverCreationJournal:
    """Recovery metadata can prohibit effects, never authorize candidate creation."""
    from coire_upgrade_executor.handover_unchanged import prohibit_candidate_effects

    authority.verify()
    owner, seed = authority.claim, preparation.incumbent
    if (
        not settings.upgrades_enabled
        or owner.project != settings.upgrade_project
        or owner.owner_identity != settings.upgrade_executor_id
        or owner.job_id is not None
        or owner.generation < preparation.generation - 1
        or (owner.executor_runtime_id, owner.executor_container_id, owner.source_sha256)
        != (seed.executor_runtime_id, seed.executor_container_id, seed.source.manifest_sha256)
    ):
        raise UpgradeForbidden("Unchanged preparation has no exact effect-free Source")
    verify_bootstrap_physical(seed, physical)
    _, journal = retain_preparation(settings, preparation)
    prohibit_candidate_effects(settings, journal, authority)
    return journal


def archive_completed_handover(
    root: PrivateUpgradeStore,
    retained: CoreHandoverPreparation,
    preparation: CoreHandoverPreparation,
) -> None:
    """Preserve the terminal generation before publishing an accepted successor."""
    current = BootstrapJournal(root.root).current()
    previous = HandoverCreationJournal.open_retained(
        root.root / "handover" / str(retained.generation), retained
    )
    unchanged = previous.store.read("failure-ack.json", CoreHandoverSummary)
    if unchanged is not None:
        verify_unchanged_files(root, previous.store, retained)
        if (
            unchanged.phase != "failed_unchanged"
            or unchanged.project != preparation.incumbent.source.manifest.project
            or unchanged.preparation_sha256 != retained.sha256()
            or unchanged.generation != retained.generation
            or unchanged.candidate_release_id != retained.candidate_release.release_id
            or unchanged.incumbent_image_id != retained.incumbent.executor_image_id
            or unchanged.candidate_image_id != retained.candidate_image_id
            or unchanged.prepared_at != retained.prepared_at
            or unchanged.deadline_at != retained.deadline_at
            or preparation.generation <= retained.generation
            or (
                preparation.incumbent.executor_runtime_id,
                preparation.incumbent.executor_container_id,
                preparation.incumbent.executor_image_id,
                preparation.incumbent.executor_source,
            )
            != (
                retained.incumbent.executor_runtime_id,
                retained.incumbent.executor_container_id,
                retained.incumbent.executor_image_id,
                retained.incumbent.executor_source,
            )
        ):
            raise UpgradeConflict(
                "Successor handover differs from its unchanged acknowledged Source"
            )
        archived_failure = previous.store.read("completed-failure.json", CoreHandoverSummary)
        if archived_failure is not None and archived_failure != unchanged:
            raise UpgradeConflict("Archived unchanged failure changed before successor publication")
        if archived_failure is None:
            previous.store.write("completed-failure.json", unchanged, replace=False)
        return
    seed = previous.retained_candidate_seed()
    if current is None or current.phase not in {"committed", "restored"}:
        raise UpgradeConflict("Another retained handover still owns recovery authority")
    key = "commit-ack.json" if current.phase == "committed" else "restoration-ack.json"
    ack = previous.store.read(key, CoreHandoverSummary)
    selected = seed if current.phase == "committed" else retained.incumbent
    if (
        ack is None
        or ack.phase != current.phase
        or ack.project != preparation.incumbent.source.manifest.project
        or ack.preparation_sha256 != retained.sha256()
        or ack.generation != retained.generation
        or ack.candidate_release_id != retained.candidate_release.release_id
        or ack.incumbent_image_id != retained.incumbent.executor_image_id
        or ack.candidate_image_id != retained.candidate_image_id
        or ack.prepared_at != retained.prepared_at
        or ack.deadline_at != retained.deadline_at
        or current.generation != retained.generation
        or (
            current.incumbent_id,
            current.incumbent_container_id,
            current.incumbent_image_id,
            current.candidate_id,
            current.candidate_container_id,
            current.candidate_image_id,
        )
        != (
            retained.incumbent.executor_runtime_id,
            retained.incumbent.executor_container_id,
            retained.incumbent.executor_image_id,
            seed.executor_runtime_id,
            seed.executor_container_id,
            seed.executor_image_id,
        )
        or preparation.generation <= retained.generation
        or (
            preparation.incumbent.executor_runtime_id,
            preparation.incumbent.executor_container_id,
            preparation.incumbent.executor_image_id,
            preparation.incumbent.executor_source,
        )
        != (
            selected.executor_runtime_id,
            selected.executor_container_id,
            selected.executor_image_id,
            selected.executor_source,
        )
    ):
        raise UpgradeConflict("Successor handover differs from acknowledged retained selection")
    archived = previous.store.read("completed-handover.json", BootstrapHandover)
    if archived is not None and archived != current:
        raise UpgradeConflict("Archived terminal handover changed before successor publication")
    if archived is None:
        previous.store.write("completed-handover.json", current, replace=False)


@tracer.start_as_current_span(
    "coire.upgrade_executor.handover_prepare",
    record_exception=False,
    set_status_on_exception=False,
)
async def prepare_passive_handover(
    settings: Settings,
    preparation: CoreHandoverPreparation,
    authority: BootstrapAuthority,
    physical: CoreBootstrapPhysicalObservation,
    client: httpx.AsyncClient,
) -> CoreBootstrapSeed:
    """Retain intent before creation; never start, select, or transfer the candidate.

    The caller obtains preparation through the authenticated accepted-handover
    claim. A journal alone cannot substitute for the caller's held incumbent.
    """
    authority.verify()
    owner = authority.claim
    incumbent = preparation.incumbent
    if (
        not settings.upgrades_enabled
        or owner.project != settings.upgrade_project
        or owner.owner_identity != settings.upgrade_executor_id
        or owner.executor_runtime_id != incumbent.executor_runtime_id
        or owner.executor_container_id != incumbent.executor_container_id
        or owner.source_sha256 != incumbent.source.manifest_sha256
        or owner.job_id is not None
        or preparation.generation > owner.generation + 1
    ):
        raise UpgradeForbidden("Passive creation has no exact incumbent ownership")
    verify_bootstrap_physical(incumbent, physical)
    root, journal = retain_preparation(settings, preparation)
    authority.verify()
    if preparation.generation != owner.generation + 1:
        from coire_upgrade_executor.handover_unchanged import prohibit_candidate_effects

        prohibit_candidate_effects(settings, journal, authority)
        raise UpgradeConflict("Restarted Source cannot resume the old candidate writer epoch")
    docker = HandoverDocker(settings, journal, authority, client)
    try:
        await docker.create(physical)
    except (CoireError, OSError, ValueError):
        journal.begin_creation_recovery()
        raise
    authority.verify()
    seed = journal.candidate_seed()
    authority.verify()
    BootstrapJournal(root.root).begin(
        BootstrapHandover(
            incumbent_id=incumbent.executor_runtime_id,
            candidate_id=seed.executor_runtime_id,
            incumbent_container_id=incumbent.executor_container_id,
            candidate_container_id=seed.executor_container_id,
            incumbent_image_id=incumbent.executor_image_id,
            candidate_image_id=seed.executor_image_id,
            generation=preparation.generation,
            phase="passive",
            updated_at=preparation.prepared_at,
            deadline_at=preparation.deadline_at,
        ),
        now=preparation.prepared_at,
    )
    authority.verify()
    return seed
