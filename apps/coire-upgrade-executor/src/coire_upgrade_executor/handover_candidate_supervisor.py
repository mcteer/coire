"""A candidate remains passive until the incumbent consumes its one writer transfer."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI
from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    BootstrapStabilityObservation,
    CoreBootstrapSeed,
    CoreHandoverCommitIntent,
    CoreHandoverCommitRequest,
    CoreHandoverCommittedRequest,
    CoreHandoverTransferIntent,
    DeploymentOwnership,
)
from coire_core.settings import Settings
from coire_upgrade_executor.bootstrap import BootstrapJournal
from coire_upgrade_executor.client import ExecutorClient
from coire_upgrade_executor.handover import HandoverObserver
from coire_upgrade_executor.handover_completion import (
    finalize_handover_commit,
    reconcile_committed_handover,
)
from coire_upgrade_executor.handover_creation import HandoverCreationJournal
from coire_upgrade_executor.handover_ownership import (
    candidate_handover_ownership,
    committed_candidate_ownership,
)
from coire_upgrade_executor.handover_selection import publish_candidate_selection
from coire_upgrade_executor.handover_source_supervisor import poll
from coire_upgrade_executor.ownership import BootstrapAuthority
from coire_upgrade_executor.passive import CandidatePassiveObserver
from coire_upgrade_executor.source_observer import InitialBootstrapObserver

tracer = trace.get_tracer("coire.upgrade_executor.handover_candidate_supervisor")


@tracer.start_as_current_span(
    "coire.upgrade_executor.handover_candidate_supervise",
    record_exception=False,
    set_status_on_exception=False,
)
async def supervise_candidate_handover(
    app: FastAPI,
    settings: Settings,
    stop: asyncio.Event,
    passive: CandidatePassiveObserver,
    api: ExecutorClient,
    http: httpx.AsyncClient,
) -> None:
    """Keep readiness false through passive, transferred, and unacknowledged states.

    Successful finalization returns to normal selected bootstrap qualification.
    An interrupted epoch never retries ownership with a renewed deadline.
    """
    app.state.bootstrap_verified = False
    app.state.bootstrap_verified_at = None
    app.state.passive_observer = passive
    creation, prep, seed = passive.journal, passive.preparation, passive.seed
    bootstrap = BootstrapJournal(Path(settings.upgrade_state_dir) / "bootstrap")
    try:
        current = bootstrap.current()
        if bootstrap.store.read("selected-seed.json", CoreBootstrapSeed) == seed:
            status = await api.handover_status(prep)
            if status.phase == "committed":
                source = InitialBootstrapObserver(
                    settings, Path(settings.upgrade_prepared_dir), seed, http
                )
                physical = await source.observe()
                with committed_candidate_ownership(
                    settings, creation, physical, status
                ) as rebooted:
                    app.state.bootstrap_authority = rebooted
                    qualified = await api.bootstrap_heartbeat(rebooted.heartbeat(seed, physical))
                    reconcile_committed_handover(
                        bootstrap.store, creation, rebooted, qualified, status
                    )
                return
        while not stop.is_set():
            transfer = creation.store.read("transfer-intent.json", CoreHandoverTransferIntent)
            if transfer is not None:
                creation.retained_transfer()
                break
            current = bootstrap.current()
            if current is not None and current.phase in {"restoring", "restored", "committed"}:
                raise UpgradeConflict(
                    "Passive candidate cannot continue a terminal or failed epoch"
                )
            if datetime.now(UTC) >= prep.deadline_at:
                raise UpgradeUnavailable("Passive candidate original preparation budget elapsed")
            await poll(stop)
        else:
            raise UpgradeConflict("Passive candidate was interrupted before transfer")
        app.state.passive_observer = None
        current = bootstrap.current()
        if current is None or current.phase != "verified":
            raise UpgradeConflict("Candidate has no independently verified transfer")
        observer = HandoverObserver(
            settings,
            Path(settings.upgrade_prepared_dir),
            prep.incumbent.executor_source,
            prep.candidate_release,
            current,
            prep.incumbent.executor_container_id,
            seed.executor_container_id,
            http,
        )
        source = InitialBootstrapObserver(settings, Path(settings.upgrade_prepared_dir), seed, http)
        while not stop.is_set():
            if datetime.now(UTC) >= transfer.deadline_at:
                raise UpgradeUnavailable("Candidate original writer capture budget elapsed")
            previous = passive.coordination.read("ownership.json", DeploymentOwnership)
            if previous is not None and previous.state == "committed":
                break
            await poll(stop)
        else:
            raise UpgradeConflict("Candidate was interrupted before writer capture")
        physical = await source.observe()
        incumbent = await observer.observe_incumbent(prep)
        with candidate_handover_ownership(settings, creation, physical, incumbent) as authority:
            app.state.bootstrap_authority = authority
            while not stop.is_set():
                authority.verify()
                current = bootstrap.current()
                if current is None or current.phase in {"restoring", "restored"}:
                    raise UpgradeConflict("Independent incumbent rejected candidate continuity")
                decision = creation.store.read("commit-decision.json", CoreHandoverCommitIntent)
                request = creation.store.read("commit-proof.json", CoreHandoverCommitRequest)
                if decision is not None:
                    if request is None or current.phase != "stable":
                        raise UpgradeConflict("Candidate commit lacks the original stable proof")
                    if datetime.now(UTC) >= decision.decided_at + timedelta(seconds=30):
                        raise UpgradeUnavailable("Candidate original commit budget elapsed")
                    physical = await source.observe()
                    incumbent = await observer.observe_incumbent(prep)
                    publish_candidate_selection(
                        bootstrap.store, creation, authority, physical, incumbent, request, decision
                    )
                    accepted = await api.acknowledge_handover_commit(
                        prep,
                        decision,
                        CoreHandoverCommittedRequest(
                            preparation_sha256=prep.sha256(),
                            candidate=authority.heartbeat(seed, physical),
                            incumbent=incumbent,
                        ),
                    )
                    physical = await source.observe()
                    incumbent = await observer.observe_incumbent(prep)
                    finalize_handover_commit(
                        bootstrap.store, creation, authority, physical, incumbent, accepted
                    )
                    return
                if current.phase == "verified" and datetime.now(UTC) >= transfer.deadline_at:
                    raise UpgradeUnavailable("Independent stability never captured the writer")
                if current.phase in {"claimed", "stable"}:
                    verify_probe_continuity(bootstrap, creation, authority)
                await poll(stop)
            raise UpgradeConflict("Candidate writer was interrupted before acknowledgment")
    finally:
        app.state.passive_observer = None
        app.state.bootstrap_authority = None
        app.state.bootstrap_verified = False
        app.state.bootstrap_verified_at = None


def verify_probe_continuity(
    bootstrap: BootstrapJournal,
    creation: HandoverCreationJournal,
    authority: BootstrapAuthority,
) -> None:
    """The phase start never substitutes for the last independent physical read."""
    proof = bootstrap.store.read("stability-observation.json", BootstrapStabilityObservation)
    prep, seed = creation.preparation, creation.retained_candidate_seed()
    started = creation.current_start()
    authority.verify()
    if (
        proof is None
        or started is None
        or started.outcome != "observed"
        or (
            proof.generation,
            proof.incumbent_id,
            proof.incumbent_container_id,
            proof.incumbent_image_id,
            proof.candidate_id,
            proof.candidate_container_id,
            proof.candidate_image_id,
            proof.kernel_boot_identity,
            proof.candidate_started_at,
        )
        != (
            prep.generation,
            prep.incumbent.executor_runtime_id,
            prep.incumbent.executor_container_id,
            prep.incumbent.executor_image_id,
            seed.executor_runtime_id,
            seed.executor_container_id,
            seed.executor_image_id,
            authority.claim.boot_identity,
            started.started_at,
        )
        or authority.claim.generation != prep.generation
        or not 0 <= (datetime.now(UTC) - proof.observed_at).total_seconds() <= 10
    ):
        raise UpgradeUnavailable("Independent stability observation continuity was lost")
