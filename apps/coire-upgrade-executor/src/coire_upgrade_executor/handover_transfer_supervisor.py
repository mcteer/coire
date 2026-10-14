"""Prepare a fixed writer transfer while the incumbent still owns its actual kernel."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx
from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import CoreHandoverPreparation, CoreHandoverTransferIntent
from coire_core.settings import Settings
from coire_upgrade_executor.bootstrap import BootstrapJournal
from coire_upgrade_executor.client import ExecutorClient
from coire_upgrade_executor.handover_creation import HandoverCreationJournal
from coire_upgrade_executor.handover_docker import HandoverDocker
from coire_upgrade_executor.handover_preparation import prepare_passive_handover
from coire_upgrade_executor.handover_protocol import observe_passive_protocol
from coire_upgrade_executor.ownership import BootstrapAuthority
from coire_upgrade_executor.source_observer import InitialBootstrapObserver

tracer = trace.get_tracer("coire.upgrade_executor.handover_transfer_supervisor")


@tracer.start_as_current_span(
    "coire.upgrade_executor.handover_transfer_prepare",
    record_exception=False,
    set_status_on_exception=False,
)
async def prepare_writer_transfer(
    settings: Settings,
    preparation: CoreHandoverPreparation,
    authority: BootstrapAuthority,
    source: InitialBootstrapObserver,
    api: ExecutorClient,
    http: httpx.AsyncClient,
    stop: asyncio.Event,
) -> CoreHandoverTransferIntent:
    """Return a fsynced consumed intent; this function never releases the Source writer.

    Any failure requires independent Source restoration before releasing its hold.
    The caller must complete that lifecycle, rather than retry an old transfer epoch.
    """
    if not settings.upgrades_enabled:
        raise UpgradeUnavailable("Managed upgrades are disabled")
    if (
        api.plan is not None
        or preparation.incumbent.source.manifest.project != settings.upgrade_project
    ):
        raise UpgradeForbidden(
            "Executor handover transfer is outside independent project authority"
        )
    authority.verify()
    if stop.is_set():
        raise UpgradeConflict("Executor handover was interrupted before passive preparation")
    physical = await source.observe()
    await prepare_passive_handover(settings, preparation, authority, physical, http)
    root = Path(settings.upgrade_state_dir) / "bootstrap"
    creation = HandoverCreationJournal.open_retained(
        root / "handover" / str(preparation.generation), preparation
    )
    journal = BootstrapJournal(root)
    docker = HandoverDocker(settings, creation, authority, http)
    if stop.is_set():
        raise UpgradeConflict("Executor handover was interrupted before candidate start")
    await docker.start_candidate(await source.observe())
    while not stop.is_set():
        authority.verify()
        if datetime.now(UTC) >= preparation.deadline_at:
            raise UpgradeUnavailable("Executor passive preparation original deadline elapsed")
        physical = await source.observe()
        await api.bootstrap_heartbeat(authority.heartbeat(preparation.incumbent, physical))
        authority.verify()
        try:
            proof = await observe_passive_protocol(docker)
        except UpgradeUnavailable:
            # Starting health and unavailable transport may settle only within
            # the original window. Identity/configuration conflicts are fatal.
            remaining = (preparation.deadline_at - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                raise
            try:
                async with asyncio.timeout(min(1, remaining)):
                    await stop.wait()
            except TimeoutError:
                pass
            continue
        creation.record_passive(proof)
        current = journal.current()
        if current is None or current.phase not in {"passive", "verified"}:
            raise UpgradeConflict("Executor transfer is outside its retained passive epoch")
        if current.phase == "passive":
            journal.advance(current, "verified", now=proof.observed_at)
        authority.verify()
        intent = await api.consume_handover_transfer(preparation, proof)
        creation.record_transfer(intent)
        authority.verify()
        if stop.is_set():
            raise UpgradeConflict("Executor handover was interrupted before Source writer release")
        if not intent.consumed_at <= datetime.now(UTC) < intent.deadline_at:
            raise UpgradeUnavailable("Executor consumed transfer original deadline elapsed")
        return intent
    raise UpgradeConflict("Executor handover was interrupted before Source writer release")
