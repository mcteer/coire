"""Accepted executor selection cannot use the ordinary application Source transition."""

import asyncio
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import UpgradeHandoverRow
from coire_core.deploy import idle_executor_owner
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    BootstrapHandover,
    CoreBootstrapHeartbeat,
    CoreBootstrapSeed,
    CoreHandoverCandidateStart,
    CoreHandoverCommitIntent,
    CoreHandoverCommitRequest,
    CoreHandoverPreparation,
    CoreHandoverRestoreRequest,
    CoreHandoverStoppedObservation,
    CoreHandoverTransferIntent,
    DeploymentHandoverHold,
    DeploymentLease,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


def verify_selected_candidate(
    settings: Settings,
    prep: CoreHandoverPreparation,
    decision: CoreHandoverCommitIntent,
    transfer: CoreHandoverTransferIntent,
    body: CoreBootstrapHeartbeat,
) -> None:
    root = Path(settings.upgrade_state_dir) / "bootstrap"
    journal_root = root / "handover" / str(prep.generation)
    coordination_root = Path(settings.upgrade_coordination_dir)
    if not all(path.exists() for path in (root, journal_root, coordination_root)):
        raise UpgradeUnavailable("Selected executor handover retained authority is absent")
    bootstrap, journal, coordination = (
        PrivateUpgradeStore(root),
        PrivateUpgradeStore(journal_root),
        PrivateUpgradeStore(coordination_root),
    )
    seed = journal.read("candidate-seed.json", CoreBootstrapSeed)
    started = journal.read("candidate-start.json", CoreHandoverCandidateStart)
    commit_proof = journal.read("commit-proof.json", CoreHandoverCommitRequest)
    current = bootstrap.read("bootstrap-handover.json", BootstrapHandover)
    hold = coordination.read("handover-hold.json", DeploymentHandoverHold)
    if any(value is None for value in (seed, started, commit_proof, current, hold)):
        raise UpgradeUnavailable("Selected executor handover evidence is incomplete")
    assert seed is not None and started is not None and commit_proof is not None
    assert current is not None and hold is not None
    if (
        body.seed != seed
        or bootstrap.read("selected-seed.json", CoreBootstrapSeed) != seed
        or bootstrap.read("handover-preparation.json", CoreHandoverPreparation) != prep
        or journal.read("preparation.json", CoreHandoverPreparation) != prep
        or journal.read("commit-decision.json", CoreHandoverCommitIntent) != decision
        or journal.read("transfer-intent.json", CoreHandoverTransferIntent) != transfer
        or seed.source != prep.incumbent.source
        or seed.source_container_ids != prep.incumbent.source_container_ids
        or seed.executor_source != prep.candidate_release
        or seed.executor_runtime_id != prep.candidate_runtime_id
        or seed.executor_image_id != prep.candidate_image_id
        or seed.generation != prep.generation
        or decision.preparation_sha256 != prep.sha256()
        or decision.candidate_seed_sha256 != seed.sha256()
        or decision.generation != prep.generation
        or decision.handover_id != transfer.handover_id
        or transfer.preparation_sha256 != prep.sha256()
        or transfer.candidate_seed_sha256 != seed.sha256()
        or transfer.generation != prep.generation
        or decision.stability_sha256 != commit_proof.stability.sha256()
        or commit_proof.preparation_sha256 != prep.sha256()
        or started.outcome != "observed"
        or started.preparation_sha256 != prep.sha256()
        or started.candidate_seed_sha256 != seed.sha256()
        or started.candidate_container_id != seed.executor_container_id
        or started.started_at != body.physical.executor_started_at
        or started.started_at != commit_proof.stability.candidate_started_at
        or started.generation != prep.generation
        or started.deadline_at != prep.deadline_at
        or current.phase != "stable"
        or current.stability_started_at is None
        or (
            current.incumbent_id,
            current.incumbent_container_id,
            current.incumbent_image_id,
            current.candidate_id,
            current.candidate_container_id,
            current.candidate_image_id,
            current.generation,
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
        or commit_proof.stability.observed_at < current.deadline_at
        or commit_proof.stability.observed_at > decision.decided_at
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
        raise UpgradeConflict("Selected executor bootstrap differs from its acknowledged decision")
    owner = idle_executor_owner(
        coordination, settings.upgrade_project, settings.upgrade_executor_id
    )
    lease = coordination.read("deployment.lease", DeploymentLease)
    if (
        owner is None
        or owner != body.ownership
        or lease != body.lease
        or owner.generation != prep.generation
        or owner.boot_identity != commit_proof.stability.kernel_boot_identity
        or lease is None
        or not transfer.consumed_at <= lease.created_at < transfer.deadline_at
        or lease.created_at > current.stability_started_at
        or idle_executor_owner(coordination, settings.upgrade_project, settings.upgrade_executor_id)
        != owner
    ):
        raise UpgradeConflict("Selected executor bootstrap lost its original held candidate kernel")


async def verify_executor_selection_transition(
    session: AsyncSession,
    retained: CoreBootstrapHeartbeat,
    body: CoreBootstrapHeartbeat,
    settings: Settings,
) -> None:
    root = Path(settings.upgrade_state_dir) / "bootstrap"
    if not root.exists():
        raise UpgradeConflict("Bootstrap incarnation changed without retained executor handover")
    prep = await asyncio.to_thread(
        PrivateUpgradeStore(root).read, "handover-preparation.json", CoreHandoverPreparation
    )
    if (
        prep is None
        or prep.incumbent != retained.seed
        or (
            body.seed.source != retained.seed.source
            or body.seed.source_container_ids != retained.seed.source_container_ids
            or body.seed.executor_source != prep.candidate_release
            or body.seed.executor_runtime_id != prep.candidate_runtime_id
            or body.seed.executor_image_id != prep.candidate_image_id
        )
    ):
        raise UpgradeConflict("Bootstrap incarnation changed outside its exact accepted handover")
    row = await session.scalar(
        select(UpgradeHandoverRow)
        .where(
            UpgradeHandoverRow.project == settings.upgrade_project,
            UpgradeHandoverRow.preparation_sha256 == prep.sha256(),
        )
        .with_for_update()
    )
    if (
        row is None
        or row.phase != "committing"
        or row.active_slot != 1
        or row.commit_payload is None
        or row.transfer_payload is None
        or CoreHandoverPreparation.model_validate(row.preparation_payload) != prep
        or row.generation != prep.generation
    ):
        raise UpgradeConflict("Bootstrap incarnation has no durable active commit decision")
    decision = CoreHandoverCommitIntent.model_validate(row.commit_payload)
    transfer = CoreHandoverTransferIntent.model_validate(row.transfer_payload)
    if decision.handover_id != row.id or transfer.handover_id != row.id:
        raise UpgradeConflict("Bootstrap incarnation mixes accepted commit identities")
    await asyncio.to_thread(verify_selected_candidate, settings, prep, decision, transfer, body)


async def verify_executor_restoration_transition(
    session: AsyncSession,
    retained: CoreBootstrapHeartbeat,
    body: CoreBootstrapHeartbeat,
    settings: Settings,
    prep: CoreHandoverPreparation,
) -> None:
    """Only the internal restoration acknowledgment may restore a selected incarnation."""
    from coire_api.upgrades.handover_restoration import verify_restoration

    root = Path(settings.upgrade_state_dir) / "bootstrap/handover" / str(prep.generation)
    if not root.exists():
        raise UpgradeConflict("Executor restoration lacks its retained handover")
    store = PrivateUpgradeStore(root)
    candidate = await asyncio.to_thread(store.read, "candidate-seed.json", CoreBootstrapSeed)
    stopped = await asyncio.to_thread(
        store.read, "candidate-stopped-observation.json", CoreHandoverStoppedObservation
    )
    if (
        candidate is None
        or stopped is None
        or retained.seed != candidate
        or body.seed != prep.incumbent
    ):
        raise UpgradeConflict("Executor restoration cannot replace another selected incarnation")
    row = await session.scalar(
        select(UpgradeHandoverRow)
        .where(
            UpgradeHandoverRow.project == settings.upgrade_project,
            UpgradeHandoverRow.preparation_sha256 == prep.sha256(),
        )
        .with_for_update()
    )
    if (
        row is None
        or row.active_slot != 1
        or row.phase not in {"committing", "restoring"}
        or row.commit_payload is None
        or row.transfer_payload is None
        or row.generation != prep.generation
        or CoreHandoverPreparation.model_validate(row.preparation_payload) != prep
    ):
        raise UpgradeConflict("Executor restoration has no uncommitted accepted selection")
    decision = CoreHandoverCommitIntent.model_validate(row.commit_payload)
    if (
        decision.handover_id != row.id
        or decision.preparation_sha256 != prep.sha256()
        or decision.candidate_seed_sha256 != candidate.sha256()
        or decision.generation != prep.generation
    ):
        raise UpgradeConflict("Executor restoration selected commit identity differs")
    await asyncio.to_thread(
        verify_restoration,
        settings,
        prep,
        CoreHandoverRestoreRequest(
            preparation_sha256=prep.sha256(),
            incumbent=body,
            candidate=stopped,
        ),
    )
