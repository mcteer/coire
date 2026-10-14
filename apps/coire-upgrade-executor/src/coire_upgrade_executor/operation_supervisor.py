"""Resume a cached accepted operation under its independent physical recovery owner."""

import asyncio
from pathlib import Path

import httpx
from opentelemetry import trace

from coire_core.deploy import host_boot_identity
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreBootstrapSeed,
    CoreRuntimeSelection,
    DeploymentOperationHold,
    DeploymentOwnership,
    UpgradeExecutionPlan,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.client import ExecutorClient
from coire_upgrade_executor.docker import UpgradeDocker
from coire_upgrade_executor.executor import CoreExecutor
from coire_upgrade_executor.journal import ExecutorJournal
from coire_upgrade_executor.ownership import executor_ownership
from coire_upgrade_executor.source_observer import InitialBootstrapObserver
from coire_upgrade_executor.worker import run_owned_operation

tracer = trace.get_tracer("coire.upgrade_executor.supervisor")


def operation_store(settings: Settings, job_id: str) -> PrivateUpgradeStore:
    root = PrivateUpgradeStore(Path(settings.upgrade_state_dir) / "operations")
    return PrivateUpgradeStore(root.path(job_id))


def cache_accepted_plan(settings: Settings, plan: UpgradeExecutionPlan) -> None:
    store = operation_store(settings, plan.operation.job_id)
    previous = store.read("accepted-plan.json", UpgradeExecutionPlan)
    if previous is not None and previous.authority_sha256() != plan.authority_sha256():
        raise UpgradeConflict(
            "Cached executor acceptance differs from immutable operation authority"
        )
    if previous is None:
        store.write("accepted-plan.json", plan, replace=False)


def cached_plan(settings: Settings, owner: DeploymentOwnership) -> UpgradeExecutionPlan:
    if owner.job_id is None:
        raise UpgradeConflict("Application recovery owner has no accepted job")
    plan = operation_store(settings, owner.job_id).read("accepted-plan.json", UpgradeExecutionPlan)
    if plan is None or (
        owner.project,
        owner.job_id,
        owner.operation_generation,
        owner.source_sha256,
    ) != (
        plan.source.manifest.project,
        plan.operation.job_id,
        plan.operation.generation,
        plan.operation.source_sha256,
    ):
        raise UpgradeUnavailable("Application recovery has no exact retained acceptance")
    return plan


@tracer.start_as_current_span(
    "coire.upgrade_executor.supervise_operation",
    record_exception=False,
    set_status_on_exception=False,
)
async def supervise_operation(
    settings: Settings,
    seed: CoreBootstrapSeed,
    plan: UpgradeExecutionPlan,
    client: ExecutorClient,
    http: httpx.AsyncClient,
    stop: asyncio.Event,
) -> None:
    if plan.source != seed.source:
        raise UpgradeConflict("Accepted operation differs from qualified bootstrap Source")
    cache_accepted_plan(settings, plan)
    client.bind(plan)
    store = operation_store(settings, plan.operation.job_id)
    journal = ExecutorJournal(store.root, plan, prepared_root=Path(settings.upgrade_prepared_dir))
    coordination = PrivateUpgradeStore(Path(settings.upgrade_coordination_dir))
    previous = coordination.read("ownership.json", DeploymentOwnership)
    if previous is None or (
        previous.owner_identity,
        previous.executor_runtime_id,
        previous.executor_container_id,
        previous.project,
    ) != (
        settings.upgrade_executor_id,
        seed.executor_runtime_id,
        seed.executor_container_id,
        settings.upgrade_project,
    ):
        raise UpgradeConflict("Application execution lacks its exact qualified independent owner")
    active_prior = previous.state == "active"
    if (
        active_prior
        and cached_plan(settings, previous).authority_sha256() != plan.authority_sha256()
    ):
        raise UpgradeConflict("Another accepted operation retains recovery ownership")
    hold = coordination.read("operation-hold.json", DeploymentOperationHold)
    if (
        hold is None
        or hold.released
        or (hold.project, hold.job_id, hold.generation, hold.plan_sha256, hold.source_sha256)
        != (
            settings.upgrade_project,
            plan.operation.job_id,
            plan.operation.generation,
            plan.authority_sha256(),
            plan.operation.source_sha256,
        )
    ):
        raise UpgradeUnavailable(
            "Application execution lacks its accepted cluster maintenance hold"
        )
    selected = coordination.read("runtime-selection.json", CoreRuntimeSelection)
    if selected is not None and selected.release_id != plan.source.release_id:
        raise UpgradeConflict("Application Source differs from retained selected runtime")
    observer = InitialBootstrapObserver(settings, Path(settings.upgrade_prepared_dir), seed, http)
    await observer.executor.verify_container(seed.executor_container_id, require_healthy=False)
    docker = UpgradeDocker(
        settings,
        plan,
        client=http,
        prepared_root=Path(settings.upgrade_prepared_dir),
        source_selection=selected,
    )
    if active_prior:
        if journal.store.path("source-snapshot.json").exists():
            snapshot = journal.source_snapshot()
            expected = {item.service: item.container_id for item in snapshot.services}
            for name in docker.source_names:
                physical = await docker.inspect_verified_source(
                    name,
                    Path(settings.upgrade_prepared_dir),
                    dependency_states={},
                    require_dependency_readiness=False,
                )
                if physical.container_id != expected.get(physical.service):
                    raise UpgradeConflict(
                        "Recovery Source container differs from retained snapshot"
                    )
        else:
            # No Source snapshot means no candidate effect could have been
            # dispatched. Still require positive whole-Source serving proof.
            await observer.source.observe()
    claim = DeploymentOwnership(
        project=settings.upgrade_project,
        owner_kind="executor",
        owner_identity=settings.upgrade_executor_id,
        executor_runtime_id=seed.executor_runtime_id,
        executor_container_id=seed.executor_container_id,
        boot_identity=host_boot_identity(),
        generation=previous.generation + 1,
        job_id=plan.operation.job_id,
        operation_generation=plan.operation.generation,
        source_sha256=plan.operation.source_sha256,
    )
    with executor_ownership(
        coordination.root, claim, plan, reconciled_prior=previous if active_prior else None
    ) as authority:
        executor = CoreExecutor(
            journal, docker, client, authority, Path(settings.upgrade_prepared_dir)
        )
        await run_owned_operation(executor, settings, stop)
