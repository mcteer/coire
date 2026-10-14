"""Independent fixed Source restoration reconciles SQL and private finalization."""

from pathlib import Path

import httpx
from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreBootstrapSeed,
    CoreHandoverCommitIntent,
    CoreHandoverCommitRequest,
    CoreHandoverCreationRecoveryIntent,
    CoreHandoverPreparation,
    CoreHandoverRecoveryRequest,
    CoreHandoverRestoreRequest,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.bootstrap import BootstrapJournal
from coire_upgrade_executor.client import ExecutorClient
from coire_upgrade_executor.handover_creation import HandoverCreationJournal
from coire_upgrade_executor.handover_recovery import HandoverRecovery, RecoveryObserver
from coire_upgrade_executor.handover_recovery_journal import HandoverRecoveryJournal
from coire_upgrade_executor.handover_restoration import finalize_restoration
from coire_upgrade_executor.namespace import verify_executor_namespace
from coire_upgrade_executor.source_observer import InitialBootstrapObserver

tracer = trace.get_tracer("coire.upgrade_executor.handover_recovery_supervisor")


@tracer.start_as_current_span(
    "coire.upgrade_executor.handover_restore",
    record_exception=False,
    set_status_on_exception=False,
)
async def restore_prepared_handover(
    settings: Settings,
    creation: HandoverCreationJournal,
    api: ExecutorClient,
    http: httpx.AsyncClient,
) -> CoreBootstrapSeed:
    """Caller must release its old writer before entry; failure leaves the hold intact.

    This routine never continues an interrupted transfer/stability epoch. It stops
    only the retained candidate and restores the original Source, even if the
    initiating human has been revoked or the accepted transfer intent has expired.
    """
    prep = creation.preparation
    if not settings.upgrades_enabled:
        raise UpgradeUnavailable("Managed upgrades are disabled")
    if prep.incumbent.source.manifest.project != settings.upgrade_project or api.plan is not None:
        raise UpgradeForbidden("Independent handover recovery is outside its fixed project")
    verify_executor_namespace(prep.incumbent)
    root = Path(settings.upgrade_state_dir) / "bootstrap"
    if not root.exists():
        raise UpgradeUnavailable("Independent handover recovery retained bootstrap is absent")
    bootstrap = BootstrapJournal(root)
    current = bootstrap.current()
    if current is None or current.phase == "committed":
        raise UpgradeConflict("Independent Source restoration lacks an uncommitted handover")
    seed = creation.retained_candidate_seed()
    if bootstrap.store.read("handover-preparation.json", CoreHandoverPreparation) != prep or (
        current.incumbent_id,
        current.incumbent_container_id,
        current.incumbent_image_id,
        current.candidate_id,
        current.candidate_container_id,
        current.candidate_image_id,
        current.generation,
    ) != (
        prep.incumbent.executor_runtime_id,
        prep.incumbent.executor_container_id,
        prep.incumbent.executor_image_id,
        seed.executor_runtime_id,
        seed.executor_container_id,
        seed.executor_image_id,
        seed.generation,
    ):
        raise UpgradeConflict("Independent Source restoration belongs to another retained handover")
    # Failure detection precedes network work; even unavailable status replies
    # cannot postpone or renew the original restoration budget.
    recovery_journal = HandoverRecoveryJournal(creation)
    creation_failure = creation.store.read(
        "creation-recovery-intent.json", CoreHandoverCreationRecoveryIntent
    )
    if creation_failure is not None and (
        creation_failure.preparation_sha256 != prep.sha256()
        or creation_failure.generation != prep.generation
    ):
        raise UpgradeConflict("Creation recovery belongs to another retained preparation")
    recovery_intent = recovery_journal.begin(
        now=creation_failure.detected_at if creation_failure is not None else None
    )
    # A lost final acknowledgment can leave the private journal stable even
    # though SQL has committed the selected candidate. Never infer rollback
    # permission from that journal, an unavailable API, or an old response.
    decision = creation.store.read("commit-decision.json", CoreHandoverCommitIntent)
    proof = creation.store.read("commit-proof.json", CoreHandoverCommitRequest)
    selected = bootstrap.store.read("selected-seed.json", CoreBootstrapSeed)
    if decision is not None or proof is not None or selected == seed:
        status = await api.handover_status(prep)
        if decision is not None and (
            decision.preparation_sha256 != prep.sha256()
            or decision.candidate_seed_sha256 != seed.sha256()
            or decision.generation != prep.generation
            or status.handover_id != decision.handover_id
        ):
            raise UpgradeConflict("Executor restoration decision belongs to another authority")
        if status.phase == "committed":
            raise UpgradeConflict(
                "Committed executor selection cannot be restored by its incumbent"
            )
        if status.phase not in {"transfer_consumed", "committing", "restoring", "restored"}:
            raise UpgradeConflict("Executor restoration has no matching pending commit authority")
    if current.phase not in {"restoring", "restored"}:
        current = bootstrap.advance(current, "restoring")
    recovery_authority = await api.begin_handover_restoration(
        prep, CoreHandoverRecoveryRequest(recovery=recovery_intent)
    )
    if decision is not None and recovery_authority.handover_id != decision.handover_id:
        raise UpgradeConflict("Executor recovery decision changed its accepted handover")
    observer = RecoveryObserver(
        settings,
        Path(settings.upgrade_prepared_dir),
        prep.incumbent.executor_source,
        prep.candidate_release,
        current,
        prep.incumbent.executor_container_id,
        seed.executor_container_id,
        http,
    )
    recovery = HandoverRecovery(
        settings,
        recovery_journal,
        observer,
        bootstrap.store,
        PrivateUpgradeStore(Path(settings.upgrade_coordination_dir)),
    )
    await recovery.stop_candidate()
    source = InitialBootstrapObserver(
        settings, Path(settings.upgrade_prepared_dir), prep.incumbent, http
    )
    async with recovery.incumbent_ownership(source) as authority:
        stopped = await recovery.observe_stopped()
        physical = await source.observe()
        body = CoreHandoverRestoreRequest(
            preparation_sha256=prep.sha256(),
            incumbent=authority.heartbeat(prep.incumbent, physical),
            candidate=stopped,
        )
        accepted = await api.acknowledge_handover_restoration(prep, body)
        authority.verify()
        # API I/O cannot refresh either physical proof or any captured deadline.
        return finalize_restoration(recovery, authority, physical, accepted)
