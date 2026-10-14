"""Accept a fresh selected candidate before durably releasing the SQL handover slot."""

import asyncio
from datetime import UTC, datetime

from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal, PrincipalKind
from coire_api.db import UpgradeHandoverRow
from coire_api.upgrades.bootstrap_heartbeat import retain_bootstrap_heartbeat
from coire_api.upgrades.handover_authority import handover_summary
from coire_api.upgrades.handover_bootstrap import verify_selected_candidate
from coire_api.upgrades.persistence import operation_lock
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreHandoverCommitIntent,
    CoreHandoverCommittedRequest,
    CoreHandoverPreparation,
    CoreHandoverSummary,
    CoreHandoverTransferIntent,
)
from coire_core.settings import Settings

tracer = trace.get_tracer("coire.api.upgrades")


@tracer.start_as_current_span(
    "coire.api.upgrade_handover_committed", record_exception=False, set_status_on_exception=False
)
async def acknowledge_handover_commit(
    session: AsyncSession,
    principal: Principal,
    request: CoreHandoverCommittedRequest,
    settings: Settings,
) -> CoreHandoverSummary:
    if not settings.upgrades_enabled:
        raise UpgradeUnavailable("Managed upgrades are disabled")
    if (
        principal.kind is not PrincipalKind.UPGRADE_EXECUTOR
        or principal.subject != settings.upgrade_executor_id
        or principal.scopes != frozenset({"upgrades:execute"})
        or request.candidate.executor_id != principal.subject
    ):
        raise UpgradeForbidden("Executor commit acknowledgment is outside restricted authority")
    await operation_lock(session)
    row = await session.scalar(
        select(UpgradeHandoverRow)
        .where(
            UpgradeHandoverRow.project == settings.upgrade_project,
            UpgradeHandoverRow.preparation_sha256 == request.preparation_sha256,
        )
        .with_for_update()
    )
    if row is None or row.commit_payload is None or row.transfer_payload is None:
        raise UpgradeConflict("Executor commit acknowledgment lacks an accepted decision")
    prep = CoreHandoverPreparation.model_validate(row.preparation_payload)
    decision = CoreHandoverCommitIntent.model_validate(row.commit_payload)
    transfer = CoreHandoverTransferIntent.model_validate(row.transfer_payload)
    incumbent = request.incumbent
    if (
        row.preparation_sha256 != prep.sha256()
        or row.generation != prep.generation
        or row.user_id != prep.initiator_user_id
        or row.idempotency_key != prep.idempotency_key
        or row.project != prep.incumbent.source.manifest.project
        or decision.handover_id != row.id
        or transfer.handover_id != row.id
        or decision.preparation_sha256 != prep.sha256()
        or decision.generation != prep.generation
        or request.candidate.seed.sha256() != decision.candidate_seed_sha256
        or incumbent.incumbent_runtime_id != prep.incumbent.executor_runtime_id
        or incumbent.incumbent_container_id != prep.incumbent.executor_container_id
        or incumbent.incumbent_image_id != prep.incumbent.executor_image_id
        or incumbent.incumbent_started_at > prep.prepared_at
    ):
        raise UpgradeConflict("Executor commit acknowledgment changed its accepted identities")
    if row.phase == "committed" and row.active_slot is None:
        return handover_summary(row)
    if row.phase != "committing" or row.active_slot != 1:
        raise UpgradeConflict("Executor commit acknowledgment lost its pending selection authority")
    if not 0 <= (datetime.now(UTC) - incumbent.observed_at).total_seconds() <= 5:
        raise UpgradeUnavailable("Executor commit retained incumbent observation expired")
    await asyncio.to_thread(
        verify_selected_candidate, settings, prep, decision, transfer, request.candidate
    )
    await retain_bootstrap_heartbeat(session, principal, request.candidate, settings)
    await asyncio.to_thread(
        verify_selected_candidate, settings, prep, decision, transfer, request.candidate
    )
    if not 0 <= (datetime.now(UTC) - incumbent.observed_at).total_seconds() <= 5:
        raise UpgradeUnavailable("Executor incumbent proof expired before commit acknowledgment")
    row.phase, row.active_slot = "committed", None
    await write_principal_audit(
        session,
        principal=principal,
        action="upgrades.handover.committed",
        target_type="upgrade_handover",
        target_id=str(row.id),
        context={"generation": row.generation, "preparation_sha256": prep.sha256()},
    )
    await session.flush()
    return handover_summary(row)
