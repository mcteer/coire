"""An effect-free handover failure retains its qualified unchanged incumbent."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal, PrincipalKind
from coire_api.db import UpgradeHandoverRow
from coire_api.upgrades.bootstrap_heartbeat import retain_bootstrap_heartbeat
from coire_api.upgrades.handover_authority import handover_summary
from coire_api.upgrades.persistence import operation_lock
from coire_core.deploy import idle_executor_owner
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreHandoverPreparation,
    CoreHandoverSummary,
    CoreHandoverUnchangedRequest,
    DeploymentHandoverHold,
    DeploymentLease,
)
from coire_core.settings import Settings
from coire_core.upgrade_handover_unchanged import verify_unchanged_files
from coire_core.upgrade_journal import PrivateUpgradeStore

tracer = trace.get_tracer("coire.api.upgrades")


def verify_unchanged_source(
    settings: Settings,
    prep: CoreHandoverPreparation,
    request: CoreHandoverUnchangedRequest,
) -> None:
    root = Path(settings.upgrade_state_dir) / "bootstrap"
    journal_root = root / "handover" / str(prep.generation)
    coordination_root = Path(settings.upgrade_coordination_dir)
    if not all(path.exists() for path in (root, journal_root, coordination_root)):
        raise UpgradeUnavailable("Effect-free handover failure retained authority is absent")
    bootstrap, journal, coordination = (
        PrivateUpgradeStore(root),
        PrivateUpgradeStore(journal_root),
        PrivateUpgradeStore(coordination_root),
    )
    intent = verify_unchanged_files(bootstrap, journal, prep)
    owner = idle_executor_owner(
        coordination, settings.upgrade_project, settings.upgrade_executor_id
    )
    hold = coordination.read("handover-hold.json", DeploymentHandoverHold)
    if (
        request.preparation_sha256 != prep.sha256()
        or request.incumbent.seed != prep.incumbent
        or owner is None
        or owner != request.incumbent.ownership
        or owner.executor_runtime_id != prep.incumbent.executor_runtime_id
        or owner.executor_container_id != prep.incumbent.executor_container_id
        or owner.source_sha256 != prep.incumbent.source.manifest_sha256
        or owner.generation < prep.generation - 1
        or coordination.read("deployment.lease", DeploymentLease) != request.incumbent.lease
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
            settings.upgrade_project,
            prep.sha256(),
            prep.generation,
            prep.incumbent.executor_runtime_id,
            prep.incumbent.executor_container_id,
            prep.candidate_runtime_id,
        )
        or request.incumbent.physical.observed_at < intent.intent_at
        or intent.intent_at > datetime.now(UTC)
    ):
        raise UpgradeConflict("Effect-free handover failure lacks its exact held unchanged Source")


@tracer.start_as_current_span(
    "coire.api.upgrade_handover_failed_unchanged",
    record_exception=False,
    set_status_on_exception=False,
)
async def acknowledge_unchanged(
    session: AsyncSession,
    principal: Principal,
    request: CoreHandoverUnchangedRequest,
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
        raise UpgradeForbidden("Effect-free failure is outside restricted service authority")
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
        raise UpgradeConflict("Effect-free failure has no accepted handover")
    prep = CoreHandoverPreparation.model_validate(row.preparation_payload)
    if (
        row.preparation_sha256 != prep.sha256()
        or row.generation != prep.generation
        or row.project != prep.incumbent.source.manifest.project
        or row.user_id != prep.initiator_user_id
        or row.idempotency_key != prep.idempotency_key
        or request.incumbent.seed != prep.incumbent
    ):
        raise UpgradeConflict("Effect-free failure accepted authority changed")
    if row.phase == "failed_unchanged" and row.active_slot is None:
        return handover_summary(row)
    if (
        row.active_slot != 1
        or row.phase not in {"accepted", "passive", "verified"}
        or row.transfer_payload is not None
        or row.commit_payload is not None
    ):
        raise UpgradeConflict("Effect-free failure cannot close consumed candidate authority")
    await asyncio.to_thread(verify_unchanged_source, settings, prep, request)
    await retain_bootstrap_heartbeat(session, principal, request.incumbent, settings)
    await asyncio.to_thread(verify_unchanged_source, settings, prep, request)
    row.phase, row.active_slot = "failed_unchanged", None
    await write_principal_audit(
        session,
        principal=principal,
        action="upgrades.handover.failed_unchanged",
        target_type="upgrade_handover",
        target_id=str(row.id),
        context={"generation": prep.generation, "preparation_sha256": prep.sha256()},
    )
    await session.flush()
    return handover_summary(row)
