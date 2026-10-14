"""The retained incumbent independently witnesses a candidate's original writer epoch."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    BootstrapHandover,
    BootstrapStabilityObservation,
    CoreHandoverCommitIntent,
    CoreHandoverCommitRequest,
    CoreHandoverSummary,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.bootstrap import BootstrapJournal
from coire_upgrade_executor.client import ExecutorClient
from coire_upgrade_executor.docker import read_docker_object
from coire_upgrade_executor.handover import (
    HandoverObserver,
    advance_handover_stability,
    start_handover_stability,
    verify_candidate_writer,
)
from coire_upgrade_executor.handover_creation import HandoverCreationJournal
from coire_upgrade_executor.handover_preparation import archive_completed_handover
from coire_upgrade_executor.handover_recovery import RecoveryObserver
from coire_upgrade_executor.namespace import verify_executor_namespace
from coire_upgrade_executor.ownership import verify_bootstrap_physical
from coire_upgrade_executor.source_observer import InitialBootstrapObserver

tracer = trace.get_tracer("coire.upgrade_executor.handover_source_supervisor")


@tracer.start_as_current_span(
    "coire.upgrade_executor.handover_creation_reconcile",
    record_exception=False,
    set_status_on_exception=False,
)
async def reconcile_dispatched_creation(
    settings: Settings,
    creation: HandoverCreationJournal,
    api: ExecutorClient,
    http: httpx.AsyncClient,
) -> None:
    """Read one original fixed name after a lost create reply; never dispatch create."""
    prep = creation.preparation
    verify_executor_namespace(prep.incumbent)
    if (
        not settings.upgrades_enabled
        or api.plan is not None
        or str(httpx.URL(settings.upgrade_proxy_url)) != "http://docker-socket-proxy:2375"
        or http.follow_redirects
        or http._trust_env
    ):
        raise UpgradeUnavailable("Creation reconciliation lacks fixed independent authority")
    current = creation.current()
    if current is None or current.outcome != "dispatched":
        raise UpgradeConflict("Creation reconciliation has no uncertain original dispatch")
    creation.begin_creation_recovery()
    status = await api.handover_status(prep)
    if status.phase not in {"accepted", "passive", "verified", "restoring"}:
        raise UpgradeConflict("Creation reconciliation has no accepted recovery authority")
    source = InitialBootstrapObserver(
        settings, Path(settings.upgrade_prepared_dir), prep.incumbent, http
    )
    verify_bootstrap_physical(prep.incumbent, await source.observe())
    body = await read_docker_object(
        http,
        httpx.URL(settings.upgrade_proxy_url),
        f"/containers/{prep.candidate_name}/json",
        allow_absent=True,
    )
    identity = body.get("Id")
    if not isinstance(identity, str) or identity == prep.incumbent.executor_container_id:
        raise UpgradeUnavailable(
            "Uncertain creation has no positive independent candidate identity"
        )
    handover = BootstrapHandover(
        incumbent_id=prep.incumbent.executor_runtime_id,
        incumbent_container_id=prep.incumbent.executor_container_id,
        incumbent_image_id=prep.incumbent.executor_image_id,
        candidate_id=prep.candidate_runtime_id,
        candidate_container_id=identity,
        candidate_image_id=prep.candidate_image_id,
        generation=prep.generation,
        phase="passive",
        updated_at=prep.prepared_at,
        deadline_at=prep.deadline_at,
    )
    observer = RecoveryObserver(
        settings,
        Path(settings.upgrade_prepared_dir),
        prep.incumbent.executor_source,
        prep.candidate_release,
        handover,
        prep.incumbent.executor_container_id,
        identity,
        http,
    )
    observer.verified_reads, observer.preparation = {}, prep
    await observer.verify_container(identity, require_healthy=False, require_running=False)
    observed = observer.verified_reads[f"/containers/{identity}/json"]
    verify_bootstrap_physical(prep.incumbent, await source.observe())
    creation.observe(creation.expected(), identity, observed_at=observed.started_at)


async def recover_observed_preparation(
    settings: Settings,
    creation: HandoverCreationJournal,
    api: ExecutorClient,
) -> None:
    """Recover a crash after observed creation but before passive journal publication.

    This records the original epoch for necessary restoration only. It never
    restarts preparation, starts a candidate, or takes a writer generation.
    """
    prep = creation.preparation
    verify_executor_namespace(prep.incumbent)
    if not settings.upgrades_enabled or api.plan is not None:
        raise UpgradeUnavailable("Observed preparation recovery lacks enabled authority")
    bootstrap = BootstrapJournal(Path(settings.upgrade_state_dir) / "bootstrap")
    if bootstrap.store.read("handover-preparation.json", type(prep)) != prep:
        raise UpgradeConflict("Observed preparation recovery belongs to another handover")
    status = await api.handover_status(prep)
    if status.phase not in {"accepted", "passive", "verified", "transfer_consumed", "restoring"}:
        raise UpgradeConflict("Observed preparation recovery cannot reconstruct terminal authority")
    current = bootstrap.current()
    if current is not None and current.generation != prep.generation:
        previous = PrivateUpgradeStore(
            bootstrap.store.root / "handover" / str(current.generation)
        ).read("preparation.json", type(prep))
        if previous is None:
            raise UpgradeConflict(
                "Observed preparation recovery lacks prior acknowledged authority"
            )
        archive_completed_handover(bootstrap.store, previous, prep)
    seed = creation.candidate_seed()
    bootstrap.begin(
        BootstrapHandover(
            incumbent_id=prep.incumbent.executor_runtime_id,
            incumbent_container_id=prep.incumbent.executor_container_id,
            incumbent_image_id=prep.incumbent.executor_image_id,
            candidate_id=seed.executor_runtime_id,
            candidate_container_id=seed.executor_container_id,
            candidate_image_id=seed.executor_image_id,
            generation=prep.generation,
            phase="passive",
            updated_at=prep.prepared_at,
            deadline_at=prep.deadline_at,
        ),
        now=prep.prepared_at,
    )


async def poll(stop: asyncio.Event) -> None:
    try:
        async with asyncio.timeout(1):
            await stop.wait()
    except TimeoutError:
        pass


@tracer.start_as_current_span(
    "coire.upgrade_executor.handover_source_supervise",
    record_exception=False,
    set_status_on_exception=False,
)
async def supervise_transferred_handover(
    settings: Settings,
    creation: HandoverCreationJournal,
    api: ExecutorClient,
    http: httpx.AsyncClient,
    stop: asyncio.Event,
) -> CoreHandoverSummary:
    """Run after Source releases its kernel; never claim it or advertise ready.

    Restarts retain the original transfer and stability deadlines. A commit
    request is fsynced before dispatch and replayed unchanged after a lost reply.
    The caller handles restoration only after this independent observer fails.
    """
    prep = creation.preparation
    verify_executor_namespace(prep.incumbent)
    if not settings.upgrades_enabled or api.plan is not None:
        raise UpgradeUnavailable("Independent handover observation lacks enabled authority")
    transfer = creation.retained_transfer()
    bootstrap = BootstrapJournal(Path(settings.upgrade_state_dir) / "bootstrap")
    current = bootstrap.current()
    seed = creation.retained_candidate_seed()
    if current is None:
        raise UpgradeUnavailable("Independent handover observation has no retained epoch")
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
    coordination = PrivateUpgradeStore(Path(settings.upgrade_coordination_dir))
    while not stop.is_set():
        incumbent = await observer.observe_incumbent(prep)
        if incumbent.incumbent_started_at > prep.prepared_at:
            raise UpgradeConflict("Restarted incumbent cannot resume a candidate stability epoch")
        current = bootstrap.current()
        if current is None or current.phase in {"passive", "restoring", "restored"}:
            raise UpgradeConflict("Independent candidate epoch cannot continue")
        if current.phase == "verified":
            if datetime.now(UTC) >= transfer.deadline_at:
                raise UpgradeUnavailable("Candidate did not capture its original transfer epoch")
            try:
                verify_candidate_writer(
                    coordination, current, settings.upgrade_project, settings.upgrade_executor_id
                )
            except (UpgradeConflict, UpgradeUnavailable):
                # The completed Source release precedes candidate capture. Do
                # not turn that ordinary interval into a failed stability epoch.
                await poll(stop)
                continue
            current = await start_handover_stability(bootstrap, observer, coordination)
        elif current.phase == "claimed":
            current = await advance_handover_stability(bootstrap, observer, coordination)
        if current.phase == "restoring":
            raise UpgradeUnavailable("Candidate independent stability observation failed")
        if current.phase == "stable":
            request = creation.store.read("commit-proof.json", CoreHandoverCommitRequest)
            if request is None:
                proof = bootstrap.store.read(
                    "stability-observation.json", BootstrapStabilityObservation
                )
                if proof is None:
                    raise UpgradeUnavailable(
                        "Stable candidate lacks its original independent proof"
                    )
                request = CoreHandoverCommitRequest(
                    preparation_sha256=prep.sha256(), stability=proof
                )
                creation.store.write("commit-proof.json", request, replace=False)
            decision = creation.store.read("commit-decision.json", CoreHandoverCommitIntent)
            if decision is None:
                decision = await api.decide_handover_commit(prep, transfer, request.stability)
                creation.store.write("commit-decision.json", decision, replace=False)
            status = await api.handover_status(prep)
            if status.handover_id != decision.handover_id:
                raise UpgradeConflict("Candidate commit status belongs to another handover")
            if status.phase == "committed":
                return status
            if status.phase != "committing":
                raise UpgradeConflict("Candidate commit decision was superseded")
            if datetime.now(UTC) >= decision.decided_at + timedelta(seconds=30):
                raise UpgradeUnavailable("Candidate commit acknowledgment original budget elapsed")
        elif current.phase == "committed":
            status = await api.handover_status(prep)
            if status.phase != "committed":
                raise UpgradeConflict("Private committed handover lacks SQL acknowledgment")
            return status
        await poll(stop)
    raise UpgradeConflict("Independent candidate observation was interrupted")
