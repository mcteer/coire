"""Durable candidate selection decision follows independently observed continuous stability."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal, PrincipalKind
from coire_api.db import UpgradeHandoverRow
from coire_api.upgrades.persistence import operation_lock
from coire_core.deploy import idle_executor_owner
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import (
    BootstrapHandover,
    BootstrapStabilityObservation,
    CoreBootstrapSeed,
    CoreHandoverCandidateStart,
    CoreHandoverCommitIntent,
    CoreHandoverCommitRequest,
    CoreHandoverCreation,
    CoreHandoverPreparation,
    CoreHandoverTransferIntent,
    DeploymentHandoverHold,
    DeploymentLease,
    DeploymentOwnership,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore

tracer = trace.get_tracer("coire.api.upgrades")


def verify_commit_candidate(
    settings: Settings,
    prep: CoreHandoverPreparation,
    transfer: CoreHandoverTransferIntent,
    request: CoreHandoverCommitRequest,
) -> tuple[CoreBootstrapSeed, DeploymentOwnership]:
    root = Path(settings.upgrade_state_dir) / "bootstrap"
    journal_root = root / "handover" / str(prep.generation)
    coordination_root = Path(settings.upgrade_coordination_dir)
    if not all(path.exists() for path in (root, journal_root, coordination_root)):
        raise UpgradeUnavailable("Executor commit retained authority is absent")
    bootstrap, journal, coordination = (
        PrivateUpgradeStore(root),
        PrivateUpgradeStore(journal_root),
        PrivateUpgradeStore(coordination_root),
    )
    seed = journal.read("candidate-seed.json", CoreBootstrapSeed)
    creation = journal.read("creation.json", CoreHandoverCreation)
    started = journal.read("candidate-start.json", CoreHandoverCandidateStart)
    handover = bootstrap.read("bootstrap-handover.json", BootstrapHandover)
    proof = bootstrap.read("stability-observation.json", BootstrapStabilityObservation)
    hold = coordination.read("handover-hold.json", DeploymentHandoverHold)
    if any(value is None for value in (seed, creation, started, handover, proof, hold)):
        raise UpgradeUnavailable("Executor commit stability evidence is incomplete")
    assert seed is not None and creation is not None and started is not None
    assert handover is not None and proof is not None and hold is not None
    if (
        bootstrap.read("handover-preparation.json", CoreHandoverPreparation) != prep
        or journal.read("preparation.json", CoreHandoverPreparation) != prep
        or journal.read("transfer-intent.json", CoreHandoverTransferIntent) != transfer
        or request.preparation_sha256 != prep.sha256()
        or request.stability != proof
        or transfer.preparation_sha256 != prep.sha256()
        or transfer.candidate_seed_sha256 != seed.sha256()
        or transfer.generation != prep.generation
        or seed.source != prep.incumbent.source
        or seed.source_container_ids != prep.incumbent.source_container_ids
        or seed.executor_source != prep.candidate_release
        or seed.executor_runtime_id != prep.candidate_runtime_id
        or seed.executor_image_id != prep.candidate_image_id
        or seed.generation != prep.generation
        or creation.outcome != "observed"
        or creation.preparation_sha256 != prep.sha256()
        or creation.candidate_container_id != seed.executor_container_id
        or creation.observed_at != seed.prepared_at
        or started.outcome != "observed"
        or started.preparation_sha256 != prep.sha256()
        or started.candidate_seed_sha256 != seed.sha256()
        or started.candidate_container_id != seed.executor_container_id
        or started.generation != seed.generation
        or started.deadline_at != prep.deadline_at
        or started.started_at != proof.candidate_started_at
        or handover.phase != "stable"
        or handover.stability_started_at is None
        or proof.candidate_started_at > handover.stability_started_at
        or proof.observed_at < handover.deadline_at
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
        or (
            proof.incumbent_id,
            proof.incumbent_container_id,
            proof.incumbent_image_id,
            proof.candidate_id,
            proof.candidate_container_id,
            proof.candidate_image_id,
            proof.generation,
        )
        != (
            handover.incumbent_id,
            handover.incumbent_container_id,
            handover.incumbent_image_id,
            handover.candidate_id,
            handover.candidate_container_id,
            handover.candidate_image_id,
            handover.generation,
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
        raise UpgradeConflict(
            "Executor commit differs from its exact independently stable candidate"
        )
    now = datetime.now(UTC)
    if not 0 <= (now - proof.observed_at).total_seconds() <= 5:
        raise UpgradeUnavailable(
            "Executor commit independent physical stability observation expired"
        )
    owner = idle_executor_owner(
        coordination, settings.upgrade_project, settings.upgrade_executor_id
    )
    lease = coordination.read("deployment.lease", DeploymentLease)
    if owner is None or (
        owner.executor_runtime_id,
        owner.executor_container_id,
        owner.generation,
        owner.boot_identity,
        owner.source_sha256,
    ) != (
        seed.executor_runtime_id,
        seed.executor_container_id,
        seed.generation,
        proof.kernel_boot_identity,
        seed.source.manifest_sha256,
    ):
        raise UpgradeConflict("Executor commit candidate has no exact held kernel writer")
    if (
        lease is None
        or not transfer.consumed_at <= lease.created_at < transfer.deadline_at
        or handover.stability_started_at is None
        or lease.created_at > handover.stability_started_at
        or idle_executor_owner(coordination, settings.upgrade_project, settings.upgrade_executor_id)
        != owner
    ):
        raise UpgradeConflict(
            "Executor commit candidate did not claim within its original transfer window"
        )
    return seed, owner


@tracer.start_as_current_span(
    "coire.api.upgrade_handover_commit_decision",
    record_exception=False,
    set_status_on_exception=False,
)
async def decide_handover_commit(
    session: AsyncSession,
    principal: Principal,
    request: CoreHandoverCommitRequest,
    settings: Settings,
) -> CoreHandoverCommitIntent:
    if not settings.upgrades_enabled:
        raise UpgradeUnavailable("Managed upgrades are disabled")
    if (
        principal.kind is not PrincipalKind.UPGRADE_EXECUTOR
        or principal.subject != settings.upgrade_executor_id
        or principal.scopes != frozenset({"upgrades:execute"})
    ):
        raise UpgradeForbidden("Executor commit is outside restricted service authority")
    await operation_lock(session)
    row = await session.scalar(
        select(UpgradeHandoverRow)
        .where(
            UpgradeHandoverRow.project == settings.upgrade_project,
            UpgradeHandoverRow.preparation_sha256 == request.preparation_sha256,
        )
        .with_for_update()
    )
    if row is None or row.transfer_payload is None:
        raise UpgradeConflict("Executor commit has no accepted consumed transfer")
    prep = CoreHandoverPreparation.model_validate(row.preparation_payload)
    transfer = CoreHandoverTransferIntent.model_validate(row.transfer_payload)
    if (
        row.generation != prep.generation
        or row.preparation_sha256 != prep.sha256()
        or row.user_id != prep.initiator_user_id
        or row.idempotency_key != prep.idempotency_key
        or row.project != prep.incumbent.source.manifest.project
        or transfer.handover_id != row.id
    ):
        raise UpgradeConflict("Executor commit accepted authority changed")
    if row.commit_payload is not None:
        decision = CoreHandoverCommitIntent.model_validate(row.commit_payload)
        if (
            row.phase not in {"committing", "committed"}
            or decision.handover_id != row.id
            or decision.preparation_sha256 != prep.sha256()
            or decision.generation != row.generation
            or decision.stability_sha256 != request.stability.sha256()
        ):
            raise UpgradeConflict("Executor commit replay differs from its retained decision")
        return decision
    if row.active_slot != 1 or row.phase not in {"transfer_consumed", "claimed", "stable"}:
        raise UpgradeConflict("Executor commit is outside its retained active transfer")
    seed, owner = await asyncio.to_thread(
        verify_commit_candidate, settings, prep, transfer, request
    )
    if await asyncio.to_thread(verify_commit_candidate, settings, prep, transfer, request) != (
        seed,
        owner,
    ):
        raise UpgradeConflict("Executor commit physical writer changed before decision")
    decision = CoreHandoverCommitIntent(
        handover_id=row.id,
        decision_id=uuid4(),
        preparation_sha256=prep.sha256(),
        candidate_seed_sha256=seed.sha256(),
        stability_sha256=request.stability.sha256(),
        generation=row.generation,
        decided_at=datetime.now(UTC),
    )
    row.phase, row.commit_payload = "committing", decision.model_dump(mode="json")
    await write_principal_audit(
        session,
        principal=principal,
        action="upgrades.handover.commit_decided",
        target_type="upgrade_handover",
        target_id=str(row.id),
        context={"generation": row.generation, "preparation_sha256": prep.sha256()},
    )
    await session.flush()
    return decision
