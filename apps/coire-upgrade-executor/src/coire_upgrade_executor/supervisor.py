"""Qualify first-install recovery independently of the application's processes."""

import asyncio
import logging
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import FastAPI

from coire_core.errors import CoireError, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreBootstrapSeed,
    CoreHandoverPreparation,
    CoreHandoverTransferIntent,
    DeploymentHandoverHold,
    DeploymentOperationHold,
    DeploymentOwnership,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.bootstrap import BootstrapJournal, prepare_initial_bootstrap
from coire_upgrade_executor.client import ExecutorClient
from coire_upgrade_executor.handover_candidate_supervisor import supervise_candidate_handover
from coire_upgrade_executor.handover_creation import HandoverCreationJournal
from coire_upgrade_executor.handover_preparation import (
    prepare_passive_handover,
    retain_unchanged_preparation,
)
from coire_upgrade_executor.handover_recovery_supervisor import restore_prepared_handover
from coire_upgrade_executor.handover_retirement import (
    observe_retired_incumbent,
    retire_incumbent,
)
from coire_upgrade_executor.handover_source_supervisor import (
    reconcile_dispatched_creation,
    recover_observed_preparation,
    supervise_transferred_handover,
)
from coire_upgrade_executor.handover_transfer_supervisor import prepare_writer_transfer
from coire_upgrade_executor.handover_unchanged import (
    abort_unchanged_handover,
    unchanged_completion_pending,
)
from coire_upgrade_executor.namespace import verify_executor_namespace
from coire_upgrade_executor.operation_supervisor import (
    cache_accepted_plan,
    cached_plan,
    supervise_operation,
)
from coire_upgrade_executor.ownership import bootstrap_ownership
from coire_upgrade_executor.passive import CandidatePassiveObserver
from coire_upgrade_executor.selected_bootstrap import qualify_selected_bootstrap
from coire_upgrade_executor.source_observer import InitialBootstrapObserver

logger = logging.getLogger(__name__)


def linux_executor_platform() -> bool:
    return sys.platform == "linux"


def clear_readiness(app: FastAPI) -> None:
    app.state.bootstrap_verified = False
    app.state.bootstrap_verified_at = None


async def wait_poll(stop: asyncio.Event, settings: Settings) -> None:
    try:
        async with asyncio.timeout(settings.upgrade_poll_s):
            await stop.wait()
    except TimeoutError:
        pass


async def supervise_passive_candidate(
    app: FastAPI,
    settings: Settings,
    stop: asyncio.Event,
    observer: CandidatePassiveObserver,
    journal: HandoverCreationJournal | None = None,
) -> None:
    """Keep the protocol available while granting no recovery or application authority.

    Ownership transfer is a separate incumbent-authorized transition. Merely
    starting a prepared candidate cannot execute that transition.
    """
    clear_readiness(app)
    app.state.passive_observer = observer
    try:
        while not stop.is_set():
            if journal is not None:
                selected = PrivateUpgradeStore(Path(settings.upgrade_state_dir) / "bootstrap").read(
                    "selected-seed.json", CoreBootstrapSeed
                )
                if (
                    journal.store.read("transfer-intent.json", CoreHandoverTransferIntent)
                    is not None
                    or selected == journal.retained_candidate_seed()
                ):
                    return
                if datetime.now(UTC) >= journal.preparation.deadline_at:
                    raise UpgradeUnavailable(
                        "Passive candidate original preparation window elapsed"
                    )
            await wait_poll(stop, settings)
    finally:
        app.state.passive_observer = None
        clear_readiness(app)


async def run_bootstrap_supervisor(app: FastAPI, settings: Settings, stop: asyncio.Event) -> None:
    """Hold a physically qualified idle writer and retain original heartbeat times.

    Retained application recovery is delegated before whole-Source health checks.
    Executor image handover remains a separate supervised boundary.
    """
    clear_readiness(app)
    if not settings.upgrades_enabled:
        # Opt-in installation must stay live while admission remains default-off.
        # Waiting grants no filesystem, transport or deployment authority.
        await stop.wait()
        return
    while not stop.is_set():
        client: ExecutorClient | None = None
        try:
            if not linux_executor_platform():
                raise UpgradeUnavailable("Core recovery supervisor requires its Linux container")
            bootstrap_store = PrivateUpgradeStore(Path(settings.upgrade_state_dir) / "bootstrap")
            seed = bootstrap_store.read(
                "selected-seed.json", CoreBootstrapSeed
            ) or bootstrap_store.read("initial-seed.json", CoreBootstrapSeed)
            if seed is None:
                seed = bootstrap_store.read("initial-seed-request.json", CoreBootstrapSeed)
                if seed is None:
                    raise UpgradeUnavailable("Initial bootstrap seed is absent")
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False, timeout=5
            ) as http:
                retirement_journal: HandoverCreationJournal | None = None
                unchanged_journal: HandoverCreationJournal | None = None
                preparation = bootstrap_store.read(
                    "handover-preparation.json", CoreHandoverPreparation
                )
                if preparation is not None:
                    root = bootstrap_store.root / "handover" / str(preparation.generation)
                    journal = HandoverCreationJournal.open_retained(root, preparation)
                    creation = journal.current()
                    if (
                        creation is not None
                        and creation.candidate_container_id is not None
                        and platform.node() == creation.candidate_container_id[:12]
                    ):
                        retained = BootstrapJournal(bootstrap_store.root).current()
                        candidate_seed = journal.retained_candidate_seed()
                        if not (
                            retained is not None
                            and retained.phase == "committed"
                            and (
                                seed.executor_runtime_id,
                                seed.executor_container_id,
                                seed.executor_image_id,
                                seed.executor_source,
                            )
                            == (
                                candidate_seed.executor_runtime_id,
                                candidate_seed.executor_container_id,
                                candidate_seed.executor_image_id,
                                candidate_seed.executor_source,
                            )
                        ):
                            observer_candidate = CandidatePassiveObserver(settings, journal, http)
                            await supervise_passive_candidate(
                                app, settings, stop, observer_candidate, journal=journal
                            )
                            if not stop.is_set():
                                client = ExecutorClient(settings)
                                await supervise_candidate_handover(
                                    app, settings, stop, observer_candidate, client, http
                                )
                            continue
                        retirement_journal = journal
                    elif platform.node() == preparation.incumbent.executor_container_id[:12]:
                        if (
                            (creation is None or creation.outcome == "intent")
                            and journal.store.path("failure-intent.json").exists()
                            and unchanged_completion_pending(settings, journal)
                        ):
                            unchanged_journal = journal
                        retained = BootstrapJournal(bootstrap_store.root).current()
                        if creation is not None and creation.outcome == "dispatched":
                            client = ExecutorClient(settings)
                            await supervise_incumbent_handover(
                                settings, journal, client, http, stop
                            )
                            continue
                        if (
                            creation is not None
                            and creation.outcome == "observed"
                            and (retained is None or retained.generation != preparation.generation)
                        ):
                            client = ExecutorClient(settings)
                            await recover_observed_preparation(settings, journal, client)
                            retained = BootstrapJournal(bootstrap_store.root).current()
                        if (
                            retained is not None
                            and retained.generation == preparation.generation
                            and retained.phase
                            in {
                                "passive",
                                "verified",
                                "claimed",
                                "stable",
                                "restoring",
                                "committed",
                            }
                        ):
                            client = client or ExecutorClient(settings)
                            await supervise_incumbent_handover(
                                settings, journal, client, http, stop
                            )
                            continue
                verify_executor_namespace(seed)
                if bootstrap_store.read("initial-seed.json", CoreBootstrapSeed) is None:
                    await prepare_initial_bootstrap(settings, seed, http)
                coordination = PrivateUpgradeStore(Path(settings.upgrade_coordination_dir))
                previous = coordination.read("ownership.json", DeploymentOwnership)
                client = ExecutorClient(settings)
                if (
                    previous is not None
                    and previous.state == "active"
                    and previous.job_id is not None
                ):
                    await supervise_operation(
                        settings, seed, cached_plan(settings, previous), client, http, stop
                    )
                    continue
                seed = await qualify_selected_bootstrap(settings, seed, http)
                observer = InitialBootstrapObserver(
                    settings, Path(settings.upgrade_prepared_dir), seed, http
                )
                observation = await observer.observe()
                accepted_plan = None
                pending_handover = None
                held = coordination.read("operation-hold.json", DeploymentOperationHold)
                handover_held = coordination.read("handover-hold.json", DeploymentHandoverHold)
                with bootstrap_ownership(
                    coordination.root,
                    seed,
                    observation,
                    settings.upgrade_executor_id,
                    reconciled_prior=previous
                    if previous is not None and previous.state == "active"
                    else None,
                    admission_hold=held if held is not None and not held.released else None,
                    handover_admission=handover_held
                    if handover_held is not None and not handover_held.released
                    else None,
                    bootstrap_root=bootstrap_store.root,
                ) as authority:
                    app.state.bootstrap_authority = authority
                    try:
                        while not stop.is_set():
                            try:
                                observation = await observer.observe()
                                body = authority.heartbeat(seed, observation)
                                accepted = await client.bootstrap_heartbeat(body)
                                authority.verify()
                                app.state.bootstrap_verified_at = accepted.physical.observed_at
                                app.state.bootstrap_verified = True
                                if unchanged_journal is not None:
                                    await abort_unchanged_handover(
                                        settings, unchanged_journal, authority, observer, client
                                    )
                                    break
                                if retirement_journal is not None:
                                    try:
                                        await observe_retired_incumbent(
                                            settings, retirement_journal, authority, http
                                        )
                                    except (CoireError, OSError, ValueError):
                                        logger.warning(
                                            "retained predecessor stop observation remains unverified"
                                        )
                                accepted_plan = await client.claim(seed.source.manifest_sha256)
                                if accepted_plan is not None:
                                    cache_accepted_plan(settings, accepted_plan)
                                    break
                                try:
                                    preparation = await client.claim_handover(seed)
                                except CoireError:
                                    recovery_preparation = await client.claim_handover_recovery(
                                        seed
                                    )
                                    if recovery_preparation is None:
                                        raise
                                    preparation = recovery_preparation
                                    unchanged = retain_unchanged_preparation(
                                        settings, preparation, authority, observation
                                    )
                                    await abort_unchanged_handover(
                                        settings, unchanged, authority, observer, client
                                    )
                                    break
                                if preparation is not None:
                                    await prepare_passive_handover(
                                        settings, preparation, authority, observation, http
                                    )
                                    if stop.is_set():
                                        break
                                    pending_handover = HandoverCreationJournal.open_retained(
                                        bootstrap_store.root
                                        / "handover"
                                        / str(preparation.generation),
                                        preparation,
                                    )
                                    try:
                                        await prepare_writer_transfer(
                                            settings,
                                            preparation,
                                            authority,
                                            observer,
                                            client,
                                            http,
                                            stop,
                                        )
                                    except (CoireError, OSError, ValueError):
                                        clear_readiness(app)
                                        logger.warning(
                                            "executor handover preparation requires restoration"
                                        )
                                    break
                            except (CoireError, OSError, ValueError) as error:
                                clear_readiness(app)
                                logger.warning(
                                    "independent Core bootstrap observation remains unverified (%s: %s)",
                                    type(error).__name__,
                                    error.detail
                                    if isinstance(error, CoireError)
                                    else "bounded local failure",
                                )
                                if preparation is not None:
                                    try:
                                        pending_handover = HandoverCreationJournal.open_retained(
                                            bootstrap_store.root
                                            / "handover"
                                            / str(preparation.generation),
                                            preparation,
                                        )
                                    except (CoireError, OSError, ValueError):
                                        pending_handover = None
                                    if pending_handover is not None:
                                        created = pending_handover.current()
                                        if created is None or created.outcome == "intent":
                                            try:
                                                await abort_unchanged_handover(
                                                    settings,
                                                    pending_handover,
                                                    authority,
                                                    observer,
                                                    client,
                                                )
                                            except (CoireError, OSError, ValueError):
                                                logger.warning(
                                                    "effect-free handover failure remains unacknowledged"
                                                )
                                            pending_handover = None
                                        break
                            await wait_poll(stop, settings)
                    finally:
                        clear_readiness(app)
                        app.state.bootstrap_authority = None
                if accepted_plan is not None:
                    await supervise_operation(settings, seed, accepted_plan, client, http, stop)
                elif pending_handover is not None and not stop.is_set():
                    await supervise_incumbent_handover(
                        settings, pending_handover, client, http, stop
                    )
        except (CoireError, OSError, ValueError) as error:
            clear_readiness(app)
            app.state.bootstrap_authority = None
            logger.warning(
                "independent Core bootstrap owner remains unavailable (%s: %s)",
                type(error).__name__,
                error.detail if isinstance(error, CoireError) else "bounded local failure",
                extra={
                    "failure_type": type(error).__name__,
                    "failure_detail": error.detail if isinstance(error, CoireError) else None,
                },
            )
        finally:
            if client is not None:
                await client.close()
        await wait_poll(stop, settings)


async def supervise_incumbent_handover(
    settings: Settings,
    journal: HandoverCreationJournal,
    api: ExecutorClient,
    http: httpx.AsyncClient,
    stop: asyncio.Event,
) -> None:
    """An incumbent remains an independent rollback owner until SQL confirms selection."""
    created = journal.current()
    if created is not None and created.outcome == "dispatched":
        await reconcile_dispatched_creation(settings, journal, api, http)
    current = BootstrapJournal(Path(settings.upgrade_state_dir) / "bootstrap").current()
    if current is None or current.generation != journal.preparation.generation:
        await recover_observed_preparation(settings, journal, api)
        current = BootstrapJournal(Path(settings.upgrade_state_dir) / "bootstrap").current()
    if current is None:
        raise UpgradeUnavailable("Incumbent handover has no retained journal")
    try:
        if current.phase == "committed":
            status = await api.handover_status(journal.preparation)
            if status.phase != "committed":
                raise UpgradeUnavailable("Private executor commit lacks SQL acknowledgment")
        else:
            await supervise_transferred_handover(settings, journal, api, http, stop)
    except (CoireError, OSError, ValueError) as error:
        logger.warning(
            "independent handover observation ended (%s: %s)",
            type(error).__name__,
            error.detail if isinstance(error, CoireError) else "bounded local failure",
        )
        # The candidate can finalize SQL selection while the incumbent's last
        # observation is in flight. A committed decision forbids restoration;
        # retirement still independently checks private acknowledgment, held
        # selected ownership and physical candidate readiness below.
        status = await api.handover_status(journal.preparation)
        if status.phase != "committed":
            await restore_prepared_handover(settings, journal, api, http)
            return
    # The predecessor stays live while candidate readiness is unproved. Stop
    # only this fixed container; retain its image/container for explicit rollback.
    while not stop.is_set():
        try:
            await retire_incumbent(settings, journal, api, http)
        except (CoireError, OSError, ValueError):
            logger.warning("executor predecessor retirement remains unverified")
        await wait_poll(stop, settings)
