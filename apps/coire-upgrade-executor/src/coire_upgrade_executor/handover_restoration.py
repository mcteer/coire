"""Finalize acknowledged restoration under the physically retained Source writer."""

from datetime import UTC, datetime

from opentelemetry import trace

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import (
    BootstrapHandover,
    CoreBootstrapPhysicalObservation,
    CoreBootstrapSeed,
    CoreHandoverSummary,
    DeploymentHandoverHold,
)
from coire_upgrade_executor.bootstrap import BootstrapJournal
from coire_upgrade_executor.handover_recovery import HandoverRecovery
from coire_upgrade_executor.ownership import BootstrapAuthority, verify_bootstrap_physical

tracer = trace.get_tracer("coire.upgrade_executor.handover_restoration")


def verify_restoration_ack(recovery: HandoverRecovery, summary: CoreHandoverSummary) -> None:
    prep = recovery.journal.creation.preparation
    if (
        summary.phase != "restored"
        or summary.project != recovery.settings.upgrade_project
        or summary.preparation_sha256 != prep.sha256()
        or summary.generation != prep.generation
        or summary.candidate_release_id != prep.candidate_release.release_id
        or summary.incumbent_image_id != prep.incumbent.executor_image_id
        or summary.candidate_image_id != prep.candidate_image_id
        or summary.prepared_at != prep.prepared_at
        or summary.deadline_at != prep.deadline_at
    ):
        raise UpgradeConflict("Executor restoration acknowledgment belongs to another handover")


@tracer.start_as_current_span(
    "coire.upgrade_executor.handover_restoration_finalize",
    record_exception=False,
    set_status_on_exception=False,
)
def finalize_restoration(
    recovery: HandoverRecovery,
    authority: BootstrapAuthority,
    physical: CoreBootstrapPhysicalObservation,
    summary: CoreHandoverSummary,
) -> CoreBootstrapSeed:
    """Persist acknowledged result before releasing the exact hold; never choose a candidate."""
    prep = recovery.journal.creation.preparation
    authority.verify()
    verify_bootstrap_physical(prep.incumbent, physical)
    verify_restoration_ack(recovery, summary)
    claim = authority.claim
    if (
        claim.project != summary.project
        or claim.owner_identity != recovery.settings.upgrade_executor_id
        or claim.executor_runtime_id != prep.incumbent.executor_runtime_id
        or claim.executor_container_id != prep.incumbent.executor_container_id
        or claim.source_sha256 != prep.incumbent.source.manifest_sha256
        or claim.generation <= prep.generation
        or claim.job_id is not None
    ):
        raise UpgradeConflict("Executor restoration finalization has no held recovered incumbent")
    with recovery.journal.store.ownership("restoration-finalize.lock"):
        retained = recovery.journal.store.read("restoration-ack.json", CoreHandoverSummary)
        if retained is not None and retained != summary:
            raise UpgradeConflict("Executor restoration acknowledgment changed on replay")
        current = recovery.bootstrap.read("bootstrap-handover.json", BootstrapHandover)
        hold = recovery.coordination.read("handover-hold.json", DeploymentHandoverHold)
        if hold is None or (
            hold.project,
            hold.preparation_sha256,
            hold.generation,
            hold.incumbent_runtime_id,
            hold.incumbent_container_id,
            hold.candidate_runtime_id,
        ) != (
            summary.project,
            prep.sha256(),
            prep.generation,
            prep.incumbent.executor_runtime_id,
            prep.incumbent.executor_container_id,
            prep.candidate_runtime_id,
        ):
            raise UpgradeConflict(
                "Executor restoration finalization lost its exact deployment hold"
            )
        if hold.released:
            if retained != summary or current is None or current.phase != "restored":
                raise UpgradeConflict(
                    "Executor released restoration hold lacks its acknowledged result"
                )
            return prep.incumbent
        recovery.verify_retained()
        authority.verify()
        if retained is None:
            recovery.journal.store.write("restoration-ack.json", summary, replace=False)
        journal = BootstrapJournal(recovery.bootstrap.root)
        if current is None or current.phase not in {"restoring", "restored"}:
            raise UpgradeConflict("Executor restoration finalization is outside its retained phase")
        if current.phase == "restoring":
            journal.advance(current, "restored")
        # The selected Source seed remains exactly the retained incarnation.
        selected = recovery.bootstrap.read("selected-seed.json", CoreBootstrapSeed)
        candidate = recovery.journal.creation.retained_candidate_seed()
        if selected is not None and selected not in (prep.incumbent, candidate):
            raise UpgradeConflict("Executor restoration cannot overwrite another selected owner")
        authority.verify()
        verify_bootstrap_physical(prep.incumbent, physical)
        if selected == candidate:
            recovery.bootstrap.write("selected-seed.json", prep.incumbent)
        authority.verify()
        recovery.coordination.write(
            "handover-hold.json",
            hold.model_copy(update={"released": True, "updated_at": datetime.now(UTC)}),
        )
        authority.verify()
        return prep.incumbent
