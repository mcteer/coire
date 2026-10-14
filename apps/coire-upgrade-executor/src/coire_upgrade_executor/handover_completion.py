"""Release only the acknowledged selected handover, while its fresh writer remains held."""

from datetime import UTC, datetime

from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    BootstrapHandover,
    CoreBootstrapHeartbeat,
    CoreBootstrapPhysicalObservation,
    CoreBootstrapSeed,
    CoreHandoverCommitIntent,
    CoreHandoverCommitRequest,
    CoreHandoverIncumbentObservation,
    CoreHandoverPreparation,
    CoreHandoverSummary,
    DeploymentHandoverHold,
)
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.handover_creation import HandoverCreationJournal
from coire_upgrade_executor.handover_selection import publish_candidate_selection
from coire_upgrade_executor.ownership import BootstrapAuthority, verify_bootstrap_physical

tracer = trace.get_tracer("coire.upgrade_executor.handover_completion")


@tracer.start_as_current_span(
    "coire.upgrade_executor.handover_commit_finalize",
    record_exception=False,
    set_status_on_exception=False,
)
def finalize_handover_commit(
    bootstrap: PrivateUpgradeStore,
    creation: HandoverCreationJournal,
    authority: BootstrapAuthority,
    physical: CoreBootstrapPhysicalObservation,
    incumbent: CoreHandoverIncumbentObservation,
    summary: CoreHandoverSummary,
) -> CoreBootstrapSeed:
    """Fresh API-acknowledged selection completes its existing epoch, never a new interval.

    Source remains restartable; this step neither stops nor deletes any container.
    An interruption before hold release is replayable with fresh physical proofs.
    """
    seed, prep = creation.retained_candidate_seed(), creation.preparation
    authority.verify()
    verify_bootstrap_physical(seed, physical)
    decision = creation.store.read("commit-decision.json", CoreHandoverCommitIntent)
    request = creation.store.read("commit-proof.json", CoreHandoverCommitRequest)
    if decision is None or request is None:
        raise UpgradeUnavailable("Executor final commit lacks its retained selection decision")
    if (
        summary.phase != "committed"
        or summary.handover_id != decision.handover_id
        or summary.project != authority.claim.project
        or summary.preparation_sha256 != prep.sha256()
        or summary.generation != prep.generation
        or summary.candidate_release_id != prep.candidate_release.release_id
        or summary.incumbent_image_id != prep.incumbent.executor_image_id
        or summary.candidate_image_id != prep.candidate_image_id
        or summary.prepared_at != prep.prepared_at
        or summary.deadline_at != prep.deadline_at
        or authority.claim.generation != seed.generation
        or authority.claim.job_id is not None
        or authority.claim.owner_kind != "executor"
        or authority.claim.stage != "preparing"
        or authority.claim.executor_runtime_id != seed.executor_runtime_id
        or authority.claim.executor_container_id != seed.executor_container_id
        or authority.claim.source_sha256 != seed.source.manifest_sha256
        or decision.preparation_sha256 != prep.sha256()
        or decision.candidate_seed_sha256 != seed.sha256()
        or decision.stability_sha256 != request.stability.sha256()
        or physical.executor_started_at != request.stability.candidate_started_at
        or authority.claim.boot_identity != request.stability.kernel_boot_identity
        or incumbent.preparation_sha256 != prep.sha256()
        or incumbent.incumbent_runtime_id != prep.incumbent.executor_runtime_id
        or incumbent.incumbent_container_id != prep.incumbent.executor_container_id
        or incumbent.incumbent_image_id != prep.incumbent.executor_image_id
        or incumbent.incumbent_started_at > prep.prepared_at
        or not 0 <= (datetime.now(UTC) - incumbent.observed_at).total_seconds() <= 5
    ):
        raise UpgradeConflict(
            "Executor final commit differs from its acknowledged held incarnation"
        )
    with creation.store.ownership("commit-finalize.lock"):
        ack = creation.store.read("commit-ack.json", CoreHandoverSummary)
        if ack is not None and ack != summary:
            raise UpgradeConflict("Executor final commit acknowledgment changed on replay")
        current = bootstrap.read("bootstrap-handover.json", BootstrapHandover)
        hold = authority.store.read("handover-hold.json", DeploymentHandoverHold)
        if (
            current is None
            or current.phase not in {"stable", "committed"}
            or bootstrap.read("handover-preparation.json", CoreHandoverPreparation) != prep
            or bootstrap.read("selected-seed.json", CoreBootstrapSeed) != seed
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
            or hold is None
            or (
                hold.project,
                hold.preparation_sha256,
                hold.generation,
                hold.incumbent_runtime_id,
                hold.incumbent_container_id,
                hold.candidate_runtime_id,
            )
            != (
                summary.project,
                prep.sha256(),
                prep.generation,
                prep.incumbent.executor_runtime_id,
                prep.incumbent.executor_container_id,
                prep.candidate_runtime_id,
            )
        ):
            raise UpgradeConflict(
                "Executor final commit lost its exact retained hold and selection"
            )
        if hold.released:
            if ack != summary or current.phase != "committed":
                raise UpgradeConflict("Executor released commit hold lacks its acknowledged result")
            return seed
        if current.phase == "stable":
            publish_candidate_selection(
                bootstrap, creation, authority, physical, incumbent, request, decision
            )
        if ack is None:
            creation.store.write("commit-ack.json", summary, replace=False)
        with bootstrap.ownership("handover.lock"):
            if bootstrap.read("bootstrap-handover.json", BootstrapHandover) != current:
                raise UpgradeConflict("Executor handover changed before acknowledged finalization")
            if current.phase == "stable":
                bootstrap.write(
                    "bootstrap-handover.json",
                    BootstrapHandover.model_validate(
                        current.model_dump()
                        | {"phase": "committed", "updated_at": datetime.now(UTC)}
                    ),
                )
        authority.verify()
        verify_bootstrap_physical(seed, physical)
        if not 0 <= (datetime.now(UTC) - incumbent.observed_at).total_seconds() <= 5:
            raise UpgradeUnavailable(
                "Executor retained incumbent proof expired before hold release"
            )
        authority.store.write(
            "handover-hold.json",
            hold.model_copy(
                update={
                    "released": True,
                    "updated_at": datetime.now(UTC),
                }
            ),
        )
        authority.verify()
        return seed


@tracer.start_as_current_span(
    "coire.upgrade_executor.handover_committed_reconcile",
    record_exception=False,
    set_status_on_exception=False,
)
def reconcile_committed_handover(
    bootstrap: PrivateUpgradeStore,
    creation: HandoverCreationJournal,
    authority: BootstrapAuthority,
    accepted: CoreBootstrapHeartbeat,
    summary: CoreHandoverSummary,
) -> CoreBootstrapSeed:
    """A fresh qualified new generation closes an already committed private hold.

    The caller obtains both typed acknowledgments through restricted API
    transport. This never resumes the transfer or alters its stability epoch.
    """
    seed, prep = creation.retained_candidate_seed(), creation.preparation
    authority.verify()
    verify_bootstrap_physical(seed, accepted.physical)
    decision = creation.store.read("commit-decision.json", CoreHandoverCommitIntent)
    request = creation.store.read("commit-proof.json", CoreHandoverCommitRequest)
    if (
        accepted != authority.heartbeat(seed, accepted.physical)
        or authority.claim.generation <= seed.generation
        or authority.claim.owner_kind != "executor"
        or authority.claim.job_id is not None
        or summary.phase != "committed"
        or summary.project != authority.claim.project
        or summary.preparation_sha256 != prep.sha256()
        or summary.generation != prep.generation
        or summary.candidate_release_id != prep.candidate_release.release_id
        or summary.incumbent_image_id != prep.incumbent.executor_image_id
        or summary.candidate_image_id != prep.candidate_image_id
        or summary.prepared_at != prep.prepared_at
        or summary.deadline_at != prep.deadline_at
        or decision is None
        or request is None
        or decision.handover_id != summary.handover_id
        or decision.preparation_sha256 != prep.sha256()
        or decision.candidate_seed_sha256 != seed.sha256()
        or decision.stability_sha256 != request.stability.sha256()
        or decision.generation != prep.generation
        or request.preparation_sha256 != prep.sha256()
        or decision.handover_id != creation.retained_transfer().handover_id
    ):
        raise UpgradeConflict("Committed reconciliation lacks its fresh qualified selection")
    with creation.store.ownership("commit-finalize.lock"):
        ack = creation.store.read("commit-ack.json", CoreHandoverSummary)
        current = bootstrap.read("bootstrap-handover.json", BootstrapHandover)
        hold = authority.store.read("handover-hold.json", DeploymentHandoverHold)
        if (
            (ack is not None and ack != summary)
            or current is None
            or current.phase not in {"stable", "committed"}
            or current.generation != prep.generation
            or current.candidate_id != seed.executor_runtime_id
            or current.candidate_container_id != seed.executor_container_id
            or current.candidate_image_id != seed.executor_image_id
            or current.incumbent_id != prep.incumbent.executor_runtime_id
            or current.incumbent_container_id != prep.incumbent.executor_container_id
            or current.incumbent_image_id != prep.incumbent.executor_image_id
            or bootstrap.read("selected-seed.json", CoreBootstrapSeed) != seed
            or bootstrap.read("handover-preparation.json", CoreHandoverPreparation) != prep
            or hold is None
            or (
                hold.project,
                hold.preparation_sha256,
                hold.generation,
                hold.incumbent_runtime_id,
                hold.incumbent_container_id,
                hold.candidate_runtime_id,
            )
            != (
                summary.project,
                prep.sha256(),
                prep.generation,
                prep.incumbent.executor_runtime_id,
                prep.incumbent.executor_container_id,
                prep.candidate_runtime_id,
            )
        ):
            raise UpgradeConflict("Committed reconciliation lost its exact private hold")
        if hold.released:
            if current.phase != "committed" or ack != summary:
                raise UpgradeConflict(
                    "Released committed reconciliation has no terminal acknowledgment"
                )
            return seed
        if ack is None:
            creation.store.write("commit-ack.json", summary, replace=False)
        with bootstrap.ownership("handover.lock"):
            if bootstrap.read("bootstrap-handover.json", BootstrapHandover) != current:
                raise UpgradeConflict("Committed reconciliation changed before private completion")
            if current.phase == "stable":
                bootstrap.write(
                    "bootstrap-handover.json",
                    BootstrapHandover.model_validate(
                        current.model_dump()
                        | {"phase": "committed", "updated_at": datetime.now(UTC)}
                    ),
                )
        authority.verify()
        verify_bootstrap_physical(seed, accepted.physical)
        authority.store.write(
            "handover-hold.json",
            hold.model_copy(
                update={
                    "released": True,
                    "updated_at": datetime.now(UTC),
                }
            ),
        )
        return seed
