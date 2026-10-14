"""Retain fresh executor bootstrap proof without granting application mutation authority."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal, PrincipalKind
from coire_api.db import UpgradeExecutorRow, UpgradeOperationRow
from coire_api.upgrades.persistence import get_release, operation_lock
from coire_api.upgrades.telemetry import logger, tracer
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreBootstrapHeartbeat,
    CoreHandoverPreparation,
    CoreRuntimeSelection,
    DeploymentOperationHold,
    UpgradeExecutionPlan,
    UpgradeOperation,
    UpgradePhase,
    UpgradeReleaseRegister,
)
from coire_core.settings import Settings
from coire_core.upgrade_artifacts import verify_prepared_release
from coire_core.upgrade_journal import PrivateUpgradeStore


async def verify_source_transition(
    session: AsyncSession,
    retained: CoreBootstrapHeartbeat,
    body: CoreBootstrapHeartbeat,
    settings: Settings,
) -> None:
    """Only a completed accepted application upgrade may change bootstrap Source."""
    old, new = retained.seed, body.seed
    if (
        new.executor_source != old.executor_source
        or new.executor_runtime_id != old.executor_runtime_id
        or new.executor_container_id != old.executor_container_id
        or new.executor_image_id != old.executor_image_id
        or new.generation <= old.generation
        or body.ownership.generation <= retained.ownership.generation
    ):
        raise UpgradeConflict("Application Source transition changed its recovery executor")
    store = PrivateUpgradeStore(Path(settings.upgrade_coordination_dir))
    hold = store.read("operation-hold.json", DeploymentOperationHold)
    selection = store.read("runtime-selection.json", CoreRuntimeSelection)
    if hold is None or not hold.released or selection is None:
        raise UpgradeConflict("Bootstrap Source transition lacks released committed selection")
    row = await session.get(UpgradeOperationRow, hold.job_id)
    if row is None or row.execution_plan is None:
        raise UpgradeConflict("Bootstrap Source transition lacks retained accepted authority")
    plan = UpgradeExecutionPlan.model_validate(row.execution_plan)
    operation = UpgradeOperation.model_validate(row.payload)
    if (
        operation.phase is not UpgradePhase.SUCCEEDED
        or operation.finished_at is None
        or operation.receipt_sequence < 1
        or row.executor_identity != body.executor_id
        or operation.authority_sha256() != plan.operation.authority_sha256()
        or plan.source != old.source
        or plan.target != new.source
        or hold.generation != plan.operation.generation
        or hold.plan_sha256 != plan.authority_sha256()
        or selection.owner_identity != body.executor_id
        or selection.project != settings.upgrade_project
        or selection.release_id != new.source.release_id
        or selection.manifest_sha256 != new.source.manifest_sha256
        or selection.compose_sha256 != new.source.manifest.compose_sha256
        or selection.generation >= body.ownership.generation
        or {item.container.service: item.container.container_id for item in selection.services}
        != new.source_container_ids
    ):
        raise UpgradeConflict(
            "Bootstrap Source transition differs from completed application upgrade"
        )


@tracer.start_as_current_span(
    "coire.api.upgrade_bootstrap_heartbeat", record_exception=False, set_status_on_exception=False
)
async def retain_bootstrap_heartbeat(
    session: AsyncSession,
    principal: Principal,
    body: CoreBootstrapHeartbeat,
    settings: Settings,
    *,
    restoration: CoreHandoverPreparation | None = None,
) -> CoreBootstrapHeartbeat:
    if not settings.upgrades_enabled:
        raise UpgradeUnavailable("Managed upgrades are disabled")
    if (
        principal.kind is not PrincipalKind.UPGRADE_EXECUTOR
        or principal.subject != settings.upgrade_executor_id
        or principal.scopes != frozenset({"upgrades:execute"})
        or body.executor_id != principal.subject
        or body.seed.source.manifest.project != settings.upgrade_project
        or body.lease.kernel_platform != "linux"
    ):
        raise UpgradeForbidden("Bootstrap heartbeat is outside the restricted executor scope")
    now = datetime.now(UTC)
    if (
        not 0 <= (now - body.physical.observed_at).total_seconds() <= 5
        or not 0 <= (now - body.physical.source.observed_at).total_seconds() <= 5
        or body.lease.created_at > now
    ):
        raise UpgradeUnavailable("Bootstrap heartbeat has no fresh original physical observation")
    await operation_lock(session)
    source = await get_release(session, body.seed.source.release_id)
    if source != body.seed.source:
        raise UpgradeConflict("Bootstrap Source differs from its registered release")
    prepared = await asyncio.to_thread(
        verify_prepared_release,
        Path(settings.upgrade_prepared_dir),
        UpgradeReleaseRegister(
            prepared_artifact_id=source.prepared_artifact_id,
            expected_sha256=source.manifest_sha256,
        ),
    )
    if prepared != source:
        raise UpgradeConflict("Bootstrap Source prepared content differs")
    if body.seed.executor_source != source:
        executor_source = await get_release(session, body.seed.executor_source.release_id)
        if executor_source != body.seed.executor_source:
            raise UpgradeConflict("Bootstrap executor differs from registered provenance")
        executor_prepared = await asyncio.to_thread(
            verify_prepared_release,
            Path(settings.upgrade_prepared_dir),
            UpgradeReleaseRegister(
                prepared_artifact_id=executor_source.prepared_artifact_id,
                expected_sha256=executor_source.manifest_sha256,
            ),
        )
        if executor_prepared != executor_source:
            raise UpgradeConflict("Bootstrap executor sealed provenance differs")
    owner = await session.get(UpgradeExecutorRow, settings.upgrade_project, with_for_update=True)
    changed = owner is None
    if owner is not None:
        if (
            owner.identity != body.executor_id
            or body.ownership.generation < owner.generation
            or owner.bootstrap_payload is None
        ):
            raise UpgradeConflict("Bootstrap heartbeat cannot replace another retained incarnation")
        retained = CoreBootstrapHeartbeat.model_validate(owner.bootstrap_payload)
        if owner.image_id != retained.seed.executor_image_id:
            raise UpgradeConflict("Retained bootstrap image differs from its qualified incarnation")
        executor_changed = (
            retained.seed.executor_source,
            retained.seed.executor_runtime_id,
            retained.seed.executor_container_id,
            retained.seed.executor_image_id,
        ) != (
            body.seed.executor_source,
            body.seed.executor_runtime_id,
            body.seed.executor_container_id,
            body.seed.executor_image_id,
        )
        source_changed = retained.seed.sha256() != body.seed.sha256()
        if executor_changed:
            from coire_api.upgrades.handover_bootstrap import (
                verify_executor_restoration_transition,
                verify_executor_selection_transition,
            )

            if restoration is None:
                await verify_executor_selection_transition(session, retained, body, settings)
            else:
                await verify_executor_restoration_transition(
                    session, retained, body, settings, restoration
                )
        elif source_changed:
            await verify_source_transition(session, retained, body, settings)
        elif owner.source_release_id != source.release_id:
            raise UpgradeConflict("Retained bootstrap Source identity differs")
        if (
            body.physical.observed_at < owner.heartbeat_at
            or (body.physical.observed_at == owner.heartbeat_at and retained != body)
            or (
                body.ownership.generation == owner.generation
                and (
                    retained.ownership != body.ownership
                    or retained.lease != body.lease
                    or retained.physical.executor_started_at != body.physical.executor_started_at
                )
            )
        ):
            raise UpgradeConflict(
                "Bootstrap heartbeat changed preparation or regressed observation"
            )
        changed = owner.generation != body.ownership.generation
    # Content verification can consume the freshness budget. Never replace the
    # original observation time with the server's current clock.
    if not 0 <= (datetime.now(UTC) - body.physical.observed_at).total_seconds() <= 5:
        raise UpgradeUnavailable("Bootstrap physical proof expired during content verification")
    if owner is None:
        owner = UpgradeExecutorRow(
            project=settings.upgrade_project,
            identity=body.executor_id,
            image_id=body.seed.executor_image_id,
            source_release_id=source.release_id,
            bootstrap_ready=True,
            bootstrap_payload=body.model_dump(mode="json"),
            heartbeat_at=body.physical.observed_at,
            generation=body.ownership.generation,
        )
        session.add(owner)
    else:
        owner.bootstrap_ready = True
        owner.image_id = body.seed.executor_image_id
        owner.source_release_id = source.release_id
        owner.bootstrap_payload = body.model_dump(mode="json")
        owner.heartbeat_at = body.physical.observed_at
        owner.generation = body.ownership.generation
    if changed:
        await write_principal_audit(
            session,
            principal=principal,
            action="upgrades.bootstrap.verified",
            target_type="upgrade_executor",
            target_id=settings.upgrade_project,
            context={
                "generation": body.ownership.generation,
                "source_sha256": source.manifest_sha256,
                "seed_sha256": body.seed.sha256(),
            },
        )
        logger.info(
            "independent Core bootstrap proof retained",
            extra={"generation": body.ownership.generation},
        )
    await session.flush()
    return body
