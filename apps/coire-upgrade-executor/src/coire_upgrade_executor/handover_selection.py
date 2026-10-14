"""Publish the exact acknowledged executor seed while retaining its candidate writer."""

from datetime import UTC, datetime

from opentelemetry import trace

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import (
    BootstrapHandover,
    CoreBootstrapPhysicalObservation,
    CoreBootstrapSeed,
    CoreHandoverCommitIntent,
    CoreHandoverCommitRequest,
    CoreHandoverIncumbentObservation,
    CoreHandoverTransferIntent,
    DeploymentHandoverHold,
)
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.handover_creation import HandoverCreationJournal
from coire_upgrade_executor.ownership import BootstrapAuthority, verify_bootstrap_physical

tracer = trace.get_tracer("coire.upgrade_executor.handover_selection")


@tracer.start_as_current_span(
    "coire.upgrade_executor.handover_selection_publish",
    record_exception=False,
    set_status_on_exception=False,
)
def publish_candidate_selection(
    bootstrap: PrivateUpgradeStore,
    creation: HandoverCreationJournal,
    authority: BootstrapAuthority,
    physical: CoreBootstrapPhysicalObservation,
    incumbent: CoreHandoverIncumbentObservation,
    request: CoreHandoverCommitRequest,
    decision: CoreHandoverCommitIntent,
) -> CoreBootstrapSeed:
    """Selection is data publication; it never retires Source or releases maintenance."""
    seed, prep = creation.retained_candidate_seed(), creation.preparation
    authority.verify()
    verify_bootstrap_physical(seed, physical)
    transfer = creation.retained_transfer()
    owner = authority.claim
    proof = request.stability
    if (
        owner.owner_kind != "executor"
        or owner.stage != "preparing"
        or owner.executor_runtime_id != seed.executor_runtime_id
        or owner.executor_container_id != seed.executor_container_id
        or owner.generation != prep.generation
        or owner.source_sha256 != prep.incumbent.source.manifest_sha256
        or owner.project != prep.incumbent.source.manifest.project
        or owner.job_id is not None
        or request.preparation_sha256 != prep.sha256()
        or decision.preparation_sha256 != prep.sha256()
        or decision.candidate_seed_sha256 != seed.sha256()
        or decision.stability_sha256 != request.stability.sha256()
        or decision.generation != prep.generation
        or decision.handover_id != transfer.handover_id
        or not proof.observed_at <= decision.decided_at <= datetime.now(UTC)
        or physical.executor_started_at != proof.candidate_started_at
        or owner.boot_identity != proof.kernel_boot_identity
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
            prep.incumbent.executor_runtime_id,
            prep.incumbent.executor_container_id,
            prep.incumbent.executor_image_id,
            seed.executor_runtime_id,
            seed.executor_container_id,
            seed.executor_image_id,
            seed.generation,
        )
        or incumbent.preparation_sha256 != prep.sha256()
        or incumbent.incumbent_runtime_id != prep.incumbent.executor_runtime_id
        or incumbent.incumbent_container_id != prep.incumbent.executor_container_id
        or incumbent.incumbent_image_id != prep.incumbent.executor_image_id
        or incumbent.incumbent_started_at > prep.prepared_at
        or not 0 <= (datetime.now(UTC) - incumbent.observed_at).total_seconds() <= 5
    ):
        raise UpgradeConflict("Candidate selection differs from its acknowledged stable handover")
    with creation.store.ownership("selection.lock"):
        current = bootstrap.read("bootstrap-handover.json", BootstrapHandover)
        hold = authority.store.read("handover-hold.json", DeploymentHandoverHold)
        if (
            bootstrap.read("handover-preparation.json", type(prep)) != prep
            or current is None
            or current.phase != "stable"
            or current.generation != prep.generation
            or current.incumbent_id != prep.incumbent.executor_runtime_id
            or current.incumbent_container_id != prep.incumbent.executor_container_id
            or current.incumbent_image_id != prep.incumbent.executor_image_id
            or current.candidate_id != seed.executor_runtime_id
            or current.candidate_container_id != seed.executor_container_id
            or current.candidate_image_id != seed.executor_image_id
            or proof.observed_at < current.deadline_at
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
                owner.project,
                prep.sha256(),
                prep.generation,
                prep.incumbent.executor_runtime_id,
                prep.incumbent.executor_container_id,
                prep.candidate_runtime_id,
            )
        ):
            raise UpgradeConflict(
                "Candidate selection lost its original handover and deployment hold"
            )
        for name, value, schema in (
            ("commit-proof.json", request, CoreHandoverCommitRequest),
            ("commit-decision.json", decision, CoreHandoverCommitIntent),
        ):
            retained = creation.store.read(name, schema)
            if retained is not None and retained != value:
                raise UpgradeConflict(
                    "Candidate selection changed its retained acknowledged decision"
                )
            if retained is None:
                creation.store.write(name, value, replace=False)
        if creation.store.read("transfer-intent.json", CoreHandoverTransferIntent) != transfer:
            raise UpgradeConflict("Candidate selection lost its consumed transfer intent")
        selected = bootstrap.read("selected-seed.json", CoreBootstrapSeed)
        if selected is not None and selected not in (prep.incumbent, seed):
            raise UpgradeConflict("Candidate selection cannot overwrite another selected owner")
        authority.verify()
        verify_bootstrap_physical(seed, physical)
        bootstrap.write("selected-seed.json", seed)
        authority.verify()
        return seed
