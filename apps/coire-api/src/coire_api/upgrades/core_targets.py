"""Core readiness requires retained SQL qualification and the still-held idle writer."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import UpgradeExecutorRow
from coire_api.upgrades.persistence import get_operation, get_release
from coire_api.upgrades.telemetry import tracer
from coire_core.deploy import idle_executor_owner
from coire_core.errors import CoireError, UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreBootstrapHeartbeat,
    CoreBootstrapSeed,
    CoreRuntimeSelection,
    DeploymentLease,
    DeploymentOperationHold,
    UpgradeAction,
    UpgradeExecutionPlan,
    UpgradeKind,
    UpgradeOperation,
    UpgradePhase,
    UpgradeTargetStatus,
    release_pair_compatible,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


def verify_current_writer(settings: Settings, body: CoreBootstrapHeartbeat) -> None:
    coordination_root = Path(settings.upgrade_coordination_dir)
    bootstrap_root = Path(settings.upgrade_state_dir) / "bootstrap"
    if not coordination_root.exists() or not bootstrap_root.exists():
        raise UpgradeUnavailable("Core readiness retained writer authority is absent")
    coordination = PrivateUpgradeStore(coordination_root)
    bootstrap = PrivateUpgradeStore(bootstrap_root)
    selected = bootstrap.read("selected-seed.json", CoreBootstrapSeed)
    if selected is None:
        selected = bootstrap.read("initial-seed.json", CoreBootstrapSeed)
    with coordination.ownership("deployment-admission.lock"):
        if (
            selected != body.seed
            or idle_executor_owner(
                coordination, settings.upgrade_project, settings.upgrade_executor_id
            )
            != body.ownership
            or coordination.read("deployment.lease", DeploymentLease) != body.lease
        ):
            raise UpgradeConflict("Core readiness no longer holds its qualified bootstrap writer")


def retained_operation(
    settings: Settings, body: CoreBootstrapHeartbeat
) -> DeploymentOperationHold | None:
    root = Path(settings.upgrade_coordination_dir)
    if not root.exists():
        return None
    store = PrivateUpgradeStore(root)
    hold = store.read("operation-hold.json", DeploymentOperationHold)
    selected = store.read("runtime-selection.json", CoreRuntimeSelection)
    if hold is None or selected is None:
        return None
    if (
        not hold.released
        or hold.project != settings.upgrade_project
        or selected.project != hold.project
        or selected.owner_identity != body.executor_id
        or selected.release_id != body.seed.source.release_id
        or selected.manifest_sha256 != body.seed.source.manifest_sha256
        or selected.generation >= body.ownership.generation
        or selected.selected_at > body.physical.observed_at
        or {item.container.service: item.container.container_id for item in selected.services}
        != body.seed.source_container_ids
        or {item.container.service: item.container.image_id for item in selected.services}
        != body.physical.source.image_ids
    ):
        raise UpgradeConflict("Core retained operation differs from its qualified selected Source")
    return hold


async def rollback_projection(
    session: AsyncSession, settings: Settings, body: CoreBootstrapHeartbeat
) -> tuple[str | None, str | None, tuple[str, ...]]:
    hold = await asyncio.to_thread(retained_operation, settings, body)
    if hold is None:
        return None, None, ()
    row = await get_operation(session, hold.job_id)
    parent = UpgradeOperation.model_validate(row.payload)
    if row.execution_plan is None:
        return None, None, ("retained_operation_unregistered",)
    plan = UpgradeExecutionPlan.model_validate(row.execution_plan)
    if (
        parent.kind is not UpgradeKind.CONTROL_PLANE
        or parent.phase is not UpgradePhase.SUCCEEDED
        or parent.finished_at is None
        or parent.receipt_sequence < 1
        or parent.job_id != hold.job_id
        or parent.generation != hold.generation
        or plan.operation.authority_sha256() != parent.authority_sha256()
        or plan.authority_sha256() != hold.plan_sha256
        or parent.source_sha256 != hold.source_sha256
        or plan.target != body.seed.source
        or parent.target_sha256 != body.seed.source.manifest_sha256
        or parent.target_release_id != body.seed.source.release_id
    ):
        return None, None, ("retained_source_identity_differs",)
    source = await get_release(session, parent.source_release_id)
    if source != plan.source:
        return None, None, ("retained_source_identity_differs",)
    if not release_pair_compatible(body.seed.source, source, UpgradeAction.ROLLBACK, parent.job_id):
        return source.manifest_sha256, None, ("retained_source_incompatible",)
    return source.manifest_sha256, parent.job_id, ()


@tracer.start_as_current_span(
    "coire.api.upgrade_core_readiness", record_exception=False, set_status_on_exception=False
)
async def core_target_status(
    session: AsyncSession, settings: Settings, host_state: str
) -> UpgradeTargetStatus:
    now = datetime.now(UTC)
    fallback = UpgradeTargetStatus(
        kind=UpgradeKind.CONTROL_PLANE,
        readiness="maintenance" if host_state == "maintenance" else "not_ready",
        blocking_reasons=(
            "cluster_operation_active"
            if host_state == "maintenance"
            else "independent_recovery_bootstrap_unready",
        ),
        observed_at=now,
    )
    if not settings.upgrades_enabled or host_state != "ready":
        return fallback
    row = await session.get(UpgradeExecutorRow, settings.upgrade_project)
    if row is None or not row.bootstrap_ready:
        return fallback
    try:
        body = CoreBootstrapHeartbeat.model_validate(row.bootstrap_payload)
        if (
            row.identity != settings.upgrade_executor_id
            or body.executor_id != row.identity
            or row.generation != body.ownership.generation
            or row.image_id != body.seed.executor_image_id
            or row.source_release_id != body.seed.source.release_id
            or row.heartbeat_at != body.physical.observed_at
            or body.seed.source.manifest.project != settings.upgrade_project
            or body.lease.kernel_platform != "linux"
            or not 0 <= (now - body.physical.observed_at).total_seconds() <= 5
            or not 0 <= (now - body.physical.source.observed_at).total_seconds() <= 5
        ):
            return fallback
        if await get_release(session, row.source_release_id) != body.seed.source:
            return fallback
        await asyncio.to_thread(verify_current_writer, settings, body)
        try:
            retained_sha, rollback_job, rollback_reasons = await rollback_projection(
                session, settings, body
            )
        except (CoireError, OSError, ValueError):
            retained_sha, rollback_job, rollback_reasons = (
                None,
                None,
                ("retained_source_unavailable",),
            )
        await asyncio.to_thread(verify_current_writer, settings, body)
        if not 0 <= (datetime.now(UTC) - body.physical.observed_at).total_seconds() <= 5:
            return fallback
    except (CoireError, OSError, ValueError):
        return fallback
    return UpgradeTargetStatus(
        kind=UpgradeKind.CONTROL_PLANE,
        readiness="ready",
        release_id=body.seed.source.release_id,
        desired_sha256=body.seed.source.manifest_sha256,
        observed_sha256=body.physical.source.source_sha256,
        service_image_ids=body.physical.source.image_ids,
        observed_at=body.physical.observed_at,
        bootstrap_ready=True,
        retained_sha256=retained_sha,
        rollback_job_id=rollback_job,
        rollback_eligible=rollback_job is not None,
        rollback_blocking_reasons=rollback_reasons,
    )
