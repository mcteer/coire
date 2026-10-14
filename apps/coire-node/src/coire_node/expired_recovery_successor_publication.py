"""Distinct successor write-ahead unready publication; original Source is unchanged."""

import threading
from datetime import UTC, datetime
from pathlib import Path

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoverySuccessorClaim,
    UpgradeExpiredRecoverySuccessorPublicationIntent,
)
from coire_core.models.upgrades import NativeGuardStatus, NativeUpgradeClaim, PreparedNativeRuntime
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_successor import require_live_successor
from coire_node.upgrade_guard import NativeGuardTransport


def publication_boundary(
    settings: Settings,
    owner: UpgradeExpiredRecoverySuccessorClaim,
    control: NativeGuardTransport,
    stopped: threading.Event | None,
) -> None:
    if stopped is not None and stopped.is_set():
        raise UpgradeUnavailable("Successor publication cancelled")
    require_live_successor(settings, owner, control)
    if stopped is not None and stopped.is_set():
        raise UpgradeUnavailable("Successor publication cancelled during boundary")


def publish_successor_unready(
    settings: Settings,
    owner: UpgradeExpiredRecoverySuccessorClaim,
    control: NativeGuardTransport,
    *,
    stopped: threading.Event | None = None,
) -> NativeGuardStatus:
    """Replay recorded preimages only, with fresh lifetime/Core/physical gates.

    Ordered runtime then ready=false status. No Source current-link, claim, receipts,
    deadlines or original maintenance changes; this is not serving qualification.
    """
    publication_boundary(settings, owner, control, stopped)
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    intent = owner.grant.observation.previous_owner.consumption.transfer.intent
    runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
    run = PrivateUpgradeStore(runs.path(intent.recovery_id), create=False)
    name = f"successor.{owner.grant.observation.succession_id}.publication.json"
    transition = run.read(name, UpgradeExpiredRecoverySuccessorPublicationIntent)
    claim = bootstrap.read("claim.json", NativeUpgradeClaim)
    runtime = bootstrap.read("runtime.json", PreparedNativeRuntime)
    status = bootstrap.read("status.json", NativeGuardStatus)
    if transition is None:
        if claim is None or runtime is None or status is None:
            raise UpgradeUnavailable("Successor publication preimages absent")
        transition = UpgradeExpiredRecoverySuccessorPublicationIntent(
            owner=owner,
            original_claim=claim,
            before_runtime=runtime,
            before_status=status,
            published_status=NativeGuardStatus(
                node=owner.grant.observation.previous_owner.consumption.transfer.legacy_guard.node,
                bootstrap_runtime=owner.runtime,
                process=owner.process,
                selected=owner.preserved_source.selected,
                ready=False,
                observed_at=datetime.now(UTC),
            ),
        )
        publication_boundary(settings, owner, control, stopped)
        run.write(name, transition, replace=False)
    if (
        transition.owner != owner
        or claim != transition.original_claim
        or runtime not in (transition.before_runtime, owner.runtime)
        or status not in (transition.before_status, transition.published_status)
    ):
        raise UpgradeConflict("Successor publication scope or preimages differ")
    if runtime != owner.runtime and status == transition.published_status:
        raise UpgradeConflict("Successor status precedes runtime")
    for filename, expected, desired, schema in (
        ("runtime.json", runtime, owner.runtime, PreparedNativeRuntime),
        ("status.json", status, transition.published_status, NativeGuardStatus),
    ):
        if expected != desired:
            publication_boundary(settings, owner, control, stopped)
            if (
                bootstrap.read("claim.json", NativeUpgradeClaim) != claim
                or bootstrap.read(filename, schema) != expected
                or bootstrap.read("runtime.json", PreparedNativeRuntime)
                != (owner.runtime if filename == "status.json" else runtime)
                or bootstrap.read("status.json", NativeGuardStatus) != status
            ):
                raise UpgradeConflict("Successor bootstrap changed before publication")
            if stopped is not None and stopped.is_set():
                raise UpgradeUnavailable("Successor publication cancelled before write")
            bootstrap.write(filename, desired)
    if (
        bootstrap.read("claim.json", NativeUpgradeClaim) != claim
        or bootstrap.read("runtime.json", PreparedNativeRuntime) != owner.runtime
        or bootstrap.read("status.json", NativeGuardStatus) != transition.published_status
    ):
        raise UpgradeConflict("Successor publication postconditions differ")
    return transition.published_status
