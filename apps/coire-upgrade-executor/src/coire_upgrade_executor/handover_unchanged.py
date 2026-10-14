"""Close a no-dispatch failure while the exact unchanged Source still holds its kernel."""

from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreHandoverSummary,
    CoreHandoverUnchangedIntent,
    CoreHandoverUnchangedRequest,
    DeploymentHandoverHold,
)
from coire_core.settings import Settings
from coire_core.upgrade_handover_unchanged import verify_unchanged_files
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.client import ExecutorClient
from coire_upgrade_executor.handover_creation import HandoverCreationJournal
from coire_upgrade_executor.namespace import verify_executor_namespace
from coire_upgrade_executor.ownership import BootstrapAuthority, verify_bootstrap_physical
from coire_upgrade_executor.source_observer import InitialBootstrapObserver

tracer = trace.get_tracer("coire.upgrade_executor.handover_unchanged")


def unchanged_completion_pending(settings: Settings, creation: HandoverCreationJournal) -> bool:
    ack = creation.store.read("failure-ack.json", CoreHandoverSummary)
    if ack is None:
        return True
    prep = creation.preparation
    if (
        ack.phase != "failed_unchanged"
        or ack.project != settings.upgrade_project
        or ack.preparation_sha256 != prep.sha256()
        or ack.generation != prep.generation
        or ack.candidate_release_id != prep.candidate_release.release_id
        or ack.incumbent_image_id != prep.incumbent.executor_image_id
        or ack.candidate_image_id != prep.candidate_image_id
        or ack.prepared_at != prep.prepared_at
        or ack.deadline_at != prep.deadline_at
    ):
        raise UpgradeConflict("Effect-free failure retained acknowledgment changed")
    root = Path(settings.upgrade_coordination_dir)
    if not root.exists():
        raise UpgradeUnavailable("Effect-free failure retained coordination is absent")
    hold = PrivateUpgradeStore(root).read("handover-hold.json", DeploymentHandoverHold)
    if hold is None:
        raise UpgradeUnavailable("Effect-free failure retained hold is absent")
    if hold.preparation_sha256 != prep.sha256():
        return False  # Successor publication required the prior released hold.
    if hold.generation != prep.generation or hold.project != settings.upgrade_project:
        raise UpgradeConflict("Effect-free failure retained hold changed")
    return not hold.released


def prohibit_candidate_effects(
    settings: Settings,
    creation: HandoverCreationJournal,
    authority: BootstrapAuthority,
) -> CoreHandoverUnchangedIntent:
    prep = creation.preparation
    verify_executor_namespace(prep.incumbent)
    authority.verify()
    owner = authority.claim
    if (
        not settings.upgrades_enabled
        or owner.job_id is not None
        or owner.owner_kind != "executor"
        or owner.project != settings.upgrade_project
        or owner.owner_identity != settings.upgrade_executor_id
        or (owner.executor_runtime_id, owner.executor_container_id, owner.source_sha256)
        != (
            prep.incumbent.executor_runtime_id,
            prep.incumbent.executor_container_id,
            prep.incumbent.source.manifest_sha256,
        )
        or owner.generation < prep.generation - 1
    ):
        raise UpgradeConflict("Effect-free failure lacks its unchanged Source writer")
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_state_dir) / "bootstrap")
    with creation.store.ownership("dispatch.lock"):
        created = creation.current()
        if created is not None and created.outcome != "intent":
            raise UpgradeConflict("Dispatched creation cannot be declared effect free")
        intent = creation.store.read("failure-intent.json", CoreHandoverUnchangedIntent)
        if intent is None:
            intent = CoreHandoverUnchangedIntent(
                preparation_sha256=prep.sha256(),
                incumbent_seed_sha256=prep.incumbent.sha256(),
                generation=prep.generation,
                intent_at=datetime.now(UTC),
            )
            creation.store.write("failure-intent.json", intent, replace=False)
        verify_unchanged_files(bootstrap, creation.store, prep)
        authority.verify()
        return intent


@tracer.start_as_current_span(
    "coire.upgrade_executor.handover_failed_unchanged",
    record_exception=False,
    set_status_on_exception=False,
)
async def abort_unchanged_handover(
    settings: Settings,
    creation: HandoverCreationJournal,
    authority: BootstrapAuthority,
    source: InitialBootstrapObserver,
    api: ExecutorClient,
) -> None:
    if api.plan is not None:
        raise UpgradeUnavailable("Effect-free failure cannot use application authority")
    prohibit_candidate_effects(settings, creation, authority)
    prep = creation.preparation
    physical = await source.observe()
    accepted = await api.acknowledge_handover_unchanged(
        prep,
        CoreHandoverUnchangedRequest(
            preparation_sha256=prep.sha256(),
            incumbent=authority.heartbeat(prep.incumbent, physical),
        ),
    )
    physical = await source.observe()
    verify_bootstrap_physical(prep.incumbent, physical)
    authority.verify()
    if (
        accepted.phase != "failed_unchanged"
        or accepted.project != settings.upgrade_project
        or accepted.preparation_sha256 != prep.sha256()
        or accepted.generation != prep.generation
        or accepted.candidate_release_id != prep.candidate_release.release_id
        or accepted.incumbent_image_id != prep.incumbent.executor_image_id
        or accepted.candidate_image_id != prep.candidate_image_id
        or accepted.prepared_at != prep.prepared_at
        or accepted.deadline_at != prep.deadline_at
    ):
        raise UpgradeConflict("Effect-free failure acknowledgment changed its accepted authority")
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_state_dir) / "bootstrap")
    with creation.store.ownership("failure-finalize.lock"):
        verify_unchanged_files(bootstrap, creation.store, prep)
        previous = creation.store.read("failure-ack.json", CoreHandoverSummary)
        hold = authority.store.read("handover-hold.json", DeploymentHandoverHold)
        if (
            (previous is not None and previous != accepted)
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
                settings.upgrade_project,
                prep.sha256(),
                prep.generation,
                prep.incumbent.executor_runtime_id,
                prep.incumbent.executor_container_id,
                prep.candidate_runtime_id,
            )
        ):
            raise UpgradeConflict("Effect-free failure lost its exact retained hold")
        if hold.released:
            if previous != accepted:
                raise UpgradeConflict("Released unchanged hold has no acknowledged outcome")
            return
        if previous is None:
            creation.store.write("failure-ack.json", accepted, replace=False)
        authority.verify()
        verify_bootstrap_physical(prep.incumbent, physical)
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
