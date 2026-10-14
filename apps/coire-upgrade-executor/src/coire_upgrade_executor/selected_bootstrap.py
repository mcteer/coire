"""Qualify a committed application cohort without changing its recovery executor."""

from datetime import UTC, datetime
from pathlib import Path

import httpx
from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    TERMINAL_UPGRADE_PHASES,
    CoreBootstrapSeed,
    CoreRuntimeSelection,
    DeploymentOperationHold,
    DeploymentOwnership,
    UpgradePhase,
    UpgradeReleaseRegister,
)
from coire_core.settings import Settings
from coire_core.upgrade_artifacts import verify_prepared_release
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.journal import ExecutorJournal
from coire_upgrade_executor.operation_supervisor import cached_plan, operation_store
from coire_upgrade_executor.source_observer import InitialBootstrapObserver

tracer = trace.get_tracer("coire.upgrade_executor.bootstrap")


@tracer.start_as_current_span(
    "coire.upgrade_executor.selected_bootstrap_qualify",
    record_exception=False,
    set_status_on_exception=False,
)
async def qualify_selected_bootstrap(
    settings: Settings, seed: CoreBootstrapSeed, client: httpx.AsyncClient
) -> CoreBootstrapSeed:
    coordination = PrivateUpgradeStore(Path(settings.upgrade_coordination_dir))
    owner = coordination.read("ownership.json", DeploymentOwnership)
    selection = coordination.read("runtime-selection.json", CoreRuntimeSelection)
    if owner is None or owner.job_id is None or selection is None:
        return seed
    if owner.state != "committed":
        return seed
    if (
        owner.owner_identity != settings.upgrade_executor_id
        or owner.executor_runtime_id != seed.executor_runtime_id
        or owner.executor_container_id != seed.executor_container_id
        or selection.owner_identity != owner.owner_identity
        or selection.project != owner.project
        or selection.generation != owner.generation
        or owner.committed_sha256 != selection.compose_sha256
    ):
        raise UpgradeConflict("Selected bootstrap differs from the committed independent owner")
    plan = cached_plan(settings, owner)
    journal = ExecutorJournal(
        operation_store(settings, owner.job_id).root,
        plan,
        prepared_root=Path(settings.upgrade_prepared_dir),
    )
    latest = journal.receipts.latest()
    hold = coordination.read("operation-hold.json", DeploymentOperationHold)
    if (
        latest is None
        or latest.phase not in TERMINAL_UPGRADE_PHASES
        or journal.receipts.acknowledged_sequence() != latest.sequence
        or hold is None
        or not hold.released
        or hold.job_id != owner.job_id
        or hold.generation != plan.operation.generation
        or hold.plan_sha256 != plan.authority_sha256()
    ):
        raise UpgradeUnavailable("Selected bootstrap lacks acknowledged terminal cleanup")
    release = plan.target if latest.phase is UpgradePhase.SUCCEEDED else plan.source
    if (
        release.release_id != selection.release_id
        or release.manifest_sha256 != selection.manifest_sha256
        or latest.observed_sha256 != release.manifest_sha256
    ):
        raise UpgradeConflict("Selected bootstrap differs from the terminal accepted release")
    containers = {
        item.container.service: item.container.container_id for item in selection.services
    }
    if seed.source == release and seed.source_container_ids == containers:
        return seed
    store = PrivateUpgradeStore(Path(settings.upgrade_state_dir) / "bootstrap")
    with store.ownership("seed.lock"):
        retained = store.read(f"selected-{owner.job_id}.json", CoreBootstrapSeed)
        qualified = retained or CoreBootstrapSeed(
            source=release,
            executor_release=seed.executor_source,
            source_container_ids=containers,
            executor_runtime_id=seed.executor_runtime_id,
            executor_container_id=seed.executor_container_id,
            executor_image_id=seed.executor_image_id,
            generation=max(seed.generation + 1, owner.generation + 1),
            prepared_at=datetime.now(UTC),
        )
        if (
            qualified.source != release
            or qualified.source_container_ids != containers
            or qualified.executor_source != seed.executor_source
            or qualified.executor_runtime_id != seed.executor_runtime_id
            or qualified.executor_container_id != seed.executor_container_id
            or qualified.executor_image_id != seed.executor_image_id
        ):
            raise UpgradeConflict("Retained selected bootstrap preparation differs")
        for artifact in (qualified.source, qualified.executor_source):
            verified = verify_prepared_release(
                Path(settings.upgrade_prepared_dir),
                UpgradeReleaseRegister(
                    prepared_artifact_id=artifact.prepared_artifact_id,
                    expected_sha256=artifact.manifest_sha256,
                ),
            )
            if verified != artifact:
                raise UpgradeConflict("Selected bootstrap sealed provenance differs")
        await InitialBootstrapObserver(
            settings, Path(settings.upgrade_prepared_dir), qualified, client
        ).observe()
        if (
            coordination.read("ownership.json", DeploymentOwnership) != owner
            or coordination.read("runtime-selection.json", CoreRuntimeSelection) != selection
            or coordination.read("operation-hold.json", DeploymentOperationHold) != hold
        ):
            raise UpgradeConflict("Committed application provenance changed during qualification")
        # Physical qualification and immutable operation provenance precede the
        # alias; the original first-install seed remains available for recovery.
        if retained is None:
            store.write(f"selected-{owner.job_id}.json", qualified, replace=False)
        store.write("selected-seed.json", qualified)
        return qualified
