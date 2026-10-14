"""Scoped holding heartbeat only; no Source bootstrap/status or engine effects."""

import logging
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import metrics, trace

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryControllerStatus,
    UpgradeExpiredRecoveryOwnerClaim,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_launchd import fixed_launchctl_probe
from coire_node.expired_recovery_preflight import verify_local_active_owner_identity
from coire_node.expired_recovery_stop import source_identity
from coire_node.recovery_identity import verify_retired_identity
from coire_node.recovery_lease import require_recovery_lease
from coire_node.upgrade_guard import NativeGuardTransport

tracer = trace.get_tracer("coire.node.recovery_controller")
logger = logging.getLogger(__name__)
observations = metrics.get_meter("coire.node.recovery_controller").create_counter(
    "coire_upgrade_recovery_controller_total",
    description="Verified holding recovery controller snapshots",
)


def run_holding_controller(
    settings: Settings,
    owner: UpgradeExpiredRecoveryOwnerClaim,
    control: NativeGuardTransport,
    stopped: threading.Event,
) -> None:
    """Bounded cancellable holding loop, no legacy budget/delegate or success.

    Immutable support entrypoint orchestration must supply the actual current
    owner; initial/resumed contexts verify its physical identity. This loop does
    not qualify serving/pairs, release maintenance or hand back to Source.
    """
    from coire_node.expired_recovery_owner import activated_recovery_owner, resumed_recovery_owner

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate_json(owner.model_dump_json())
    intent = owner.consumption.transfer.intent
    if (
        not settings.upgrades_enabled
        or settings.node_name not in {"coire-edge-a", "coire-edge-b"}
        or settings.node_name != intent.original_plan.operation.node
    ):
        raise UpgradeUnavailable("Holding recovery controller is not authorized on this node")
    if stopped.is_set():
        return
    if datetime.now(UTC) >= intent.deadline_at:
        raise UpgradeUnavailable("Holding controller captured recovery budget expired")
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
    PrivateUpgradeStore(runs.path(intent.recovery_id), create=False)
    state = control.probe_recovery_owner(owner)
    if stopped.is_set():
        return
    if state.owner is not None and state.owner.sha256() != owner.sha256():
        raise UpgradeUnavailable("Holding controller accepted owner state differs")
    context = resumed_recovery_owner if state.owner is not None else activated_recovery_owner
    with context(settings, owner.consumption, owner.runtime, control) as current:
        if current.sha256() != owner.sha256():
            raise UpgradeUnavailable("Holding controller owner changed")
        run_held_controller(settings, current, control, stopped)


def run_held_controller(
    settings: Settings,
    owner: UpgradeExpiredRecoveryOwnerClaim,
    control: NativeGuardTransport,
    stopped: threading.Event,
) -> None:
    """Run inside an already activated lifetime lease, without releasing/relocking.

    Initial support sessions can transition into this loop while keeping actual
    exclusion continuously held. A retained claim alone never authorizes it.
    """
    from coire_node.expired_recovery_publication import publish_unready_support

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate_json(owner.model_dump_json())
    intent = owner.consumption.transfer.intent
    if stopped.is_set():
        return
    if (
        not settings.upgrades_enabled
        or settings.node_name not in {"coire-edge-a", "coire-edge-b"}
        or settings.node_name != intent.original_plan.operation.node
        or datetime.now(UTC) >= intent.deadline_at
    ):
        raise UpgradeUnavailable("Held recovery controller scope or budget differs")
    deadline_tick = time.monotonic() + max(
        0.0, min(300.0, (intent.deadline_at - datetime.now(UTC)).total_seconds())
    )

    def remaining_budget() -> float:
        return min(
            (intent.deadline_at - datetime.now(UTC)).total_seconds(),
            deadline_tick - time.monotonic(),
        )

    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    require_recovery_lease(bootstrap, owner.sha256())
    publish_unready_support(settings, owner, control)
    while not stopped.is_set():
        remaining = remaining_budget()
        if remaining <= 0:
            raise UpgradeUnavailable("Holding controller captured recovery budget expired")
        require_recovery_lease(bootstrap, owner.sha256())
        publish_holding_controller(settings, owner, control)
        if stopped.is_set():
            return
        remaining = remaining_budget()
        if remaining <= 0:
            raise UpgradeUnavailable("Holding controller heartbeat exhausted captured budget")
        stopped.wait(min(1.0, remaining))


def publish_holding_controller(
    settings: Settings,
    claim: UpgradeExpiredRecoveryOwnerClaim,
    control: NativeGuardTransport,
) -> UpgradeExpiredRecoveryControllerStatus:
    """Fresh holding observation inside accepted owner lifetime, never ready=true.

    Actual publication into legacy bootstrap requires a separately journalled
    transition/controller protocol. A historical heartbeat cannot replace Core
    reauthorization, physical owner proof or fresh qualification at later effects.
    """
    with tracer.start_as_current_span("coire.node.recovery_controller_snapshot"):
        bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
        require_recovery_lease(bootstrap, claim.sha256())
        if control.verify_recovery_owner(claim).sha256() != claim.sha256():
            raise UpgradeUnavailable("Controller has no exact live Core owner")
        verify_local_active_owner_identity(settings, claim)
        service = claim.legacy_stop.effect.service
        if service is None:
            raise UpgradeUnavailable("Controller has no positive managed retirement")
        verify_retired_identity(claim.consumption.transfer.legacy_guard.process)
        if (
            fixed_launchctl_probe(claim.consumption.transfer.intent.deadline_at)(service.target)
            is not None
        ):
            raise UpgradeUnavailable("Legacy service is loaded during controller observation")
        if source_identity(settings, claim.consumption) != claim.legacy_stop.preserved_source:
            raise UpgradeUnavailable("Source changed before controller observation")
        runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
        run = PrivateUpgradeStore(
            runs.path(claim.consumption.transfer.intent.recovery_id), create=False
        )
        accepted = run.read("owner.activation.json", UpgradeExpiredRecoveryOwnerClaim)
        if accepted is None or accepted.sha256() != claim.sha256():
            raise UpgradeUnavailable("Controller has no exact retained acknowledgement")
        require_recovery_lease(bootstrap, claim.sha256())
        if control.verify_recovery_owner(claim).sha256() != claim.sha256():
            raise UpgradeUnavailable("Controller authority changed before heartbeat")
        snapshot = UpgradeExpiredRecoveryControllerStatus(
            owner=claim, observed_at=datetime.now(UTC)
        )
        run.write("controller.status.json", snapshot)
        observations.add(1, {"outcome": "holding", "node": settings.node_name})
        logger.info(
            "Recovery controller holding; not serving qualification",
            extra={
                "job_id": claim.consumption.transfer.intent.request.original_job_id,
                "recovery_id": claim.consumption.transfer.intent.recovery_id,
                "node": settings.node_name,
                "outcome": "holding",
            },
        )
        return snapshot
