"""Close independent restoration only while the exact incumbent holds a fresh writer."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal, PrincipalKind
from coire_api.db import UpgradeExecutorRow, UpgradeHandoverRow
from coire_api.upgrades.bootstrap_heartbeat import retain_bootstrap_heartbeat
from coire_api.upgrades.handover_authority import handover_summary
from coire_api.upgrades.persistence import operation_lock
from coire_core.deploy import idle_executor_owner
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import (
    BootstrapHandover,
    CoreBootstrapSeed,
    CoreHandoverCreation,
    CoreHandoverPreparation,
    CoreHandoverRecoveryIntent,
    CoreHandoverRecoveryRequest,
    CoreHandoverRestoreRequest,
    CoreHandoverStoppedObservation,
    CoreHandoverSummary,
    DeploymentHandoverHold,
    DeploymentLease,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore

tracer = trace.get_tracer("coire.api.upgrades")


def verify_recovery_request(
    settings: Settings, prep: CoreHandoverPreparation, request: CoreHandoverRecoveryRequest
) -> None:
    root = Path(settings.upgrade_state_dir) / "bootstrap"
    journal_root = root / "handover" / str(prep.generation)
    coordination_root = Path(settings.upgrade_coordination_dir)
    if not all(path.exists() for path in (root, journal_root, coordination_root)):
        raise UpgradeUnavailable("Executor recovery retained authority is absent")
    bootstrap, journal, coordination = (
        PrivateUpgradeStore(root),
        PrivateUpgradeStore(journal_root),
        PrivateUpgradeStore(coordination_root),
    )
    seed = journal.read("candidate-seed.json", CoreBootstrapSeed)
    handover = bootstrap.read("bootstrap-handover.json", BootstrapHandover)
    hold = coordination.read("handover-hold.json", DeploymentHandoverHold)
    intent = request.recovery
    if (
        seed is None
        or handover is None
        or hold is None
        or bootstrap.read("handover-preparation.json", CoreHandoverPreparation) != prep
        or journal.read("preparation.json", CoreHandoverPreparation) != prep
        or journal.read("recovery-intent.json", CoreHandoverRecoveryIntent) != intent
        or intent.preparation_sha256 != prep.sha256()
        or intent.candidate_seed_sha256 != seed.sha256()
        or intent.generation != prep.generation
        or intent.incumbent_container_id != prep.incumbent.executor_container_id
        or intent.candidate_container_id != seed.executor_container_id
        or seed.source != prep.incumbent.source
        or seed.executor_source != prep.candidate_release
        or seed.executor_runtime_id != prep.candidate_runtime_id
        or seed.executor_image_id != prep.candidate_image_id
        or seed.generation != prep.generation
        or handover.phase != "restoring"
        or (
            handover.incumbent_id,
            handover.incumbent_container_id,
            handover.incumbent_image_id,
            handover.candidate_id,
            handover.candidate_container_id,
            handover.candidate_image_id,
            handover.generation,
        )
        != (
            prep.incumbent.executor_runtime_id,
            prep.incumbent.executor_container_id,
            prep.incumbent.executor_image_id,
            seed.executor_runtime_id,
            seed.executor_container_id,
            seed.executor_image_id,
            prep.generation,
        )
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
            prep.sha256(),
            prep.generation,
            prep.incumbent.executor_runtime_id,
            prep.incumbent.executor_container_id,
            prep.candidate_runtime_id,
        )
    ):
        raise UpgradeConflict("Executor recovery request differs from its retained authority")
    if not intent.detected_at <= datetime.now(UTC) < intent.deadline_at:
        raise UpgradeUnavailable("Executor recovery original budget is expired or future")


@tracer.start_as_current_span(
    "coire.api.upgrade_handover_restoring", record_exception=False, set_status_on_exception=False
)
async def begin_restoration(
    session: AsyncSession,
    principal: Principal,
    request: CoreHandoverRecoveryRequest,
    settings: Settings,
) -> CoreHandoverSummary:
    """Serialize restoration against final SQL commit before any candidate stop."""
    if not settings.upgrades_enabled:
        raise UpgradeUnavailable("Managed upgrades are disabled")
    if (
        principal.kind is not PrincipalKind.UPGRADE_EXECUTOR
        or principal.subject != settings.upgrade_executor_id
        or principal.scopes != frozenset({"upgrades:execute"})
    ):
        raise UpgradeForbidden("Executor recovery is outside restricted service authority")
    await operation_lock(session)
    row = await session.scalar(
        select(UpgradeHandoverRow)
        .where(
            UpgradeHandoverRow.project == settings.upgrade_project,
            UpgradeHandoverRow.preparation_sha256 == request.recovery.preparation_sha256,
        )
        .with_for_update()
    )
    if row is None:
        raise UpgradeConflict("Executor recovery has no accepted handover")
    prep = CoreHandoverPreparation.model_validate(row.preparation_payload)
    if (
        row.generation != prep.generation
        or row.preparation_sha256 != prep.sha256()
        or row.user_id != prep.initiator_user_id
        or row.idempotency_key != prep.idempotency_key
        or row.project != prep.incumbent.source.manifest.project
    ):
        raise UpgradeConflict("Executor recovery accepted authority changed")
    if row.phase == "restored" and row.active_slot is None:
        return handover_summary(row)
    if row.active_slot != 1 or row.phase in {"committed", "failed_unchanged"}:
        raise UpgradeConflict("Terminal executor selection cannot begin restoration")
    await asyncio.to_thread(verify_recovery_request, settings, prep, request)
    if row.phase != "restoring":
        row.phase = "restoring"
        await write_principal_audit(
            session,
            principal=principal,
            action="upgrades.handover.restoring",
            target_type="upgrade_handover",
            target_id=str(row.id),
            context={"generation": prep.generation, "preparation_sha256": prep.sha256()},
        )
    await session.flush()
    return handover_summary(row)


def verify_restoration(
    settings: Settings, prep: CoreHandoverPreparation, request: CoreHandoverRestoreRequest
) -> None:
    root = Path(settings.upgrade_state_dir) / "bootstrap"
    coordination_root = Path(settings.upgrade_coordination_dir)
    journal_root = root / "handover" / str(prep.generation)
    if not all(path.exists() for path in (root, coordination_root, journal_root)):
        raise UpgradeUnavailable("Executor restoration retained authority is absent")
    bootstrap, coordination, journal = (
        PrivateUpgradeStore(root),
        PrivateUpgradeStore(coordination_root),
        PrivateUpgradeStore(journal_root),
    )
    seed = journal.read("candidate-seed.json", CoreBootstrapSeed)
    created = journal.read("creation.json", CoreHandoverCreation)
    recovery = journal.read("recovery-intent.json", CoreHandoverRecoveryIntent)
    stopped = journal.read("candidate-stopped-observation.json", CoreHandoverStoppedObservation)
    handover = bootstrap.read("bootstrap-handover.json", BootstrapHandover)
    hold = coordination.read("handover-hold.json", DeploymentHandoverHold)
    if any(value is None for value in (seed, created, recovery, stopped, handover, hold)):
        raise UpgradeUnavailable("Executor restoration evidence is incomplete")
    assert seed is not None and created is not None and recovery is not None
    assert stopped is not None and handover is not None and hold is not None
    if (
        bootstrap.read("handover-preparation.json", CoreHandoverPreparation) != prep
        or journal.read("preparation.json", CoreHandoverPreparation) != prep
        or request.preparation_sha256 != prep.sha256()
        or request.incumbent.seed != prep.incumbent
        or request.candidate != stopped
        or seed.source != prep.incumbent.source
        or seed.source_container_ids != prep.incumbent.source_container_ids
        or seed.executor_source != prep.candidate_release
        or seed.executor_runtime_id != prep.candidate_runtime_id
        or seed.executor_image_id != prep.candidate_image_id
        or seed.generation != prep.generation
        or created.outcome != "observed"
        or created.preparation_sha256 != prep.sha256()
        or created.candidate_container_id != seed.executor_container_id
        or created.observed_at != seed.prepared_at
        or recovery.outcome != "stopped"
        or recovery.preparation_sha256 != prep.sha256()
        or recovery.candidate_seed_sha256 != seed.sha256()
        or recovery.incumbent_container_id != prep.incumbent.executor_container_id
        or recovery.candidate_container_id != seed.executor_container_id
        or recovery.generation != prep.generation
        or stopped.preparation_sha256 != prep.sha256()
        or stopped.candidate_seed_sha256 != seed.sha256()
        or stopped.candidate_container_id != seed.executor_container_id
        or stopped.generation != prep.generation
        or handover.phase != "restoring"
        or (
            handover.incumbent_id,
            handover.incumbent_container_id,
            handover.incumbent_image_id,
            handover.candidate_id,
            handover.candidate_container_id,
            handover.candidate_image_id,
            handover.generation,
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
            prep.sha256(),
            prep.generation,
            prep.incumbent.executor_runtime_id,
            prep.incumbent.executor_container_id,
            prep.candidate_runtime_id,
        )
    ):
        raise UpgradeConflict("Executor restoration differs from its exact retained handover")
    owner = idle_executor_owner(
        coordination, settings.upgrade_project, settings.upgrade_executor_id
    )
    if (
        owner is None
        or owner != request.incumbent.ownership
        or owner.generation <= prep.generation
        or owner.executor_runtime_id != prep.incumbent.executor_runtime_id
        or owner.executor_container_id != prep.incumbent.executor_container_id
        or owner.source_sha256 != prep.incumbent.source.manifest_sha256
        or coordination.read("deployment.lease", DeploymentLease) != request.incumbent.lease
    ):
        raise UpgradeConflict("Executor restoration lacks the fresh exact incumbent kernel writer")
    now = datetime.now(UTC)
    if not 0 <= (now - stopped.observed_at).total_seconds() <= 5:
        raise UpgradeUnavailable("Executor restoration positive candidate stop observation expired")


@tracer.start_as_current_span(
    "coire.api.upgrade_handover_restored", record_exception=False, set_status_on_exception=False
)
async def acknowledge_restoration(
    session: AsyncSession,
    principal: Principal,
    request: CoreHandoverRestoreRequest,
    settings: Settings,
) -> CoreHandoverSummary:
    if not settings.upgrades_enabled:
        raise UpgradeUnavailable("Managed upgrades are disabled")
    if (
        principal.kind is not PrincipalKind.UPGRADE_EXECUTOR
        or principal.subject != settings.upgrade_executor_id
        or principal.scopes != frozenset({"upgrades:execute"})
        or request.incumbent.executor_id != principal.subject
    ):
        raise UpgradeForbidden("Executor restoration is outside restricted service authority")
    await operation_lock(session)
    row = await session.scalar(
        select(UpgradeHandoverRow)
        .where(
            UpgradeHandoverRow.project == settings.upgrade_project,
            UpgradeHandoverRow.preparation_sha256 == request.preparation_sha256,
        )
        .with_for_update()
    )
    if row is None:
        raise UpgradeConflict("Executor restoration has no accepted handover")
    prep = CoreHandoverPreparation.model_validate(row.preparation_payload)
    if (
        row.generation != prep.generation
        or row.preparation_sha256 != prep.sha256()
        or row.user_id != prep.initiator_user_id
        or row.idempotency_key != prep.idempotency_key
        or row.project != prep.incumbent.source.manifest.project
        or request.incumbent.seed != prep.incumbent
        or request.candidate.generation != prep.generation
    ):
        raise UpgradeConflict("Executor restoration accepted preparation changed")
    if row.phase == "restored" and row.active_slot is None:
        return handover_summary(row)
    if row.active_slot != 1 or row.phase != "restoring":
        raise UpgradeConflict("Executor handover has no serialized restoration decision")
    await asyncio.to_thread(verify_restoration, settings, prep, request)
    # Necessary restoration survives human revocation; no new activation is authorized.
    owner_row = await session.get(
        UpgradeExecutorRow, settings.upgrade_project, with_for_update=True
    )
    if owner_row is not None and owner_row.image_id != request.incumbent.seed.executor_image_id:
        await retain_bootstrap_heartbeat(
            session, principal, request.incumbent, settings, restoration=prep
        )
    else:
        await retain_bootstrap_heartbeat(session, principal, request.incumbent, settings)
    await asyncio.to_thread(verify_restoration, settings, prep, request)
    row.phase, row.active_slot = "restored", None
    await write_principal_audit(
        session,
        principal=principal,
        action="upgrades.handover.restored",
        target_type="upgrade_handover",
        target_id=str(row.id),
        context={
            "generation": request.incumbent.ownership.generation,
            "preparation_sha256": prep.sha256(),
        },
    )
    await session.flush()
    return handover_summary(row)
