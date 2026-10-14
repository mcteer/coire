"""Distinct successor holding loop: no Source publication or qualification."""

import logging
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import metrics, trace

from coire_core.errors import CoireError, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoverySuccessorClaim,
    UpgradeExpiredRecoverySuccessorControllerStatus,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_successor import (
    initial_successor_owner,
    require_live_successor,
    resumed_successor_owner,
)
from coire_node.upgrade_guard import NativeGuardTransport

tracer = trace.get_tracer("coire.node.recovery_successor_controller")
logger = logging.getLogger(__name__)
observations = metrics.get_meter("coire.node.recovery_successor_controller").create_counter(
    "coire_upgrade_recovery_successor_controller_total",
    description="Distinct successor holding observations, never qualification",
)


def refuse_expired_budget(
    settings: Settings, claim: UpgradeExpiredRecoverySuccessorClaim, reason: str
) -> None:
    intent = claim.grant.observation.previous_owner.consumption.transfer.intent
    observations.add(1, {"outcome": "refused", "node": settings.node_name})
    logger.warning(
        "Successor controller budget refused",
        extra={
            "job_id": intent.request.original_job_id,
            "recovery_id": intent.recovery_id,
            "succession_id": claim.grant.observation.succession_id,
            "node": settings.node_name,
            "outcome": "refused",
            "reason": "budget_expired",
        },
    )
    raise UpgradeUnavailable(reason)


def verify_controller_boundary(
    settings: Settings, claim: UpgradeExpiredRecoverySuccessorClaim, control: NativeGuardTransport
) -> None:
    try:
        require_live_successor(settings, claim, control)
    except CoireError:
        observations.add(1, {"outcome": "refused", "node": settings.node_name})
        intent = claim.grant.observation.previous_owner.consumption.transfer.intent
        logger.warning(
            "Successor controller authority refused",
            extra={
                "job_id": intent.request.original_job_id,
                "recovery_id": intent.recovery_id,
                "succession_id": claim.grant.observation.succession_id,
                "node": settings.node_name,
                "outcome": "refused",
            },
        )
        raise


def run_successor_holding(
    settings: Settings,
    claim: UpgradeExpiredRecoverySuccessorClaim,
    control: NativeGuardTransport,
    stopped: threading.Event,
) -> None:
    """Explicit accepted/pending selection; no exception-based legacy fallback."""
    if stopped.is_set():
        return
    state = control.probe_recovery_successor(claim)
    if stopped.is_set():
        return
    context = resumed_successor_owner if state.owner is not None else initial_successor_owner
    if state.owner is not None and state.owner != claim:
        raise UpgradeUnavailable("Successor holding accepted state differs")
    with context(settings, claim, control) as current:
        run_held_successor(settings, current, control, stopped)


def run_held_successor(
    settings: Settings,
    claim: UpgradeExpiredRecoverySuccessorClaim,
    control: NativeGuardTransport,
    stopped: threading.Event,
) -> None:
    """Remain inside actual lifetime exclusion through each bounded heartbeat.

    Separate status only, ready=false. Does not publish legacy runtime/status,
    qualify serving/pairs/data, mutate original history or release maintenance.
    """
    claim = UpgradeExpiredRecoverySuccessorClaim.model_validate_json(claim.model_dump_json())
    intent = claim.grant.observation.previous_owner.consumption.transfer.intent
    if (
        not settings.upgrades_enabled
        or settings.node_name not in {"coire-edge-a", "coire-edge-b"}
        or settings.node_name != intent.original_plan.operation.node
    ):
        raise UpgradeUnavailable("Successor controller is not authorized on this node")
    deadline_tick = time.monotonic() + max(
        0.0, min(300.0, (intent.deadline_at - datetime.now(UTC)).total_seconds())
    )

    def remaining_budget() -> float:
        return min(
            (intent.deadline_at - datetime.now(UTC)).total_seconds(),
            deadline_tick - time.monotonic(),
        )

    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
    run = PrivateUpgradeStore(runs.path(intent.recovery_id), create=False)
    if stopped.is_set():
        return
    if remaining_budget() <= 0:
        refuse_expired_budget(settings, claim, "Successor controller original budget expired")
    from coire_node.expired_recovery_successor_publication import publish_successor_unready

    publish_successor_unready(settings, claim, control, stopped=stopped)
    while not stopped.is_set():
        with tracer.start_as_current_span("coire.node.recovery_successor_controller_snapshot"):
            if remaining_budget() <= 0:
                refuse_expired_budget(
                    settings, claim, "Successor controller original budget expired"
                )
            verify_controller_boundary(settings, claim, control)
            if stopped.is_set():
                return
            snapshot = UpgradeExpiredRecoverySuccessorControllerStatus(
                owner=claim, observed_at=datetime.now(UTC)
            )
            verify_controller_boundary(settings, claim, control)
            if stopped.is_set():
                return
            if remaining_budget() <= 0:
                refuse_expired_budget(
                    settings, claim, "Successor controller budget expired before heartbeat"
                )
            run.write(f"successor.{claim.grant.observation.succession_id}.status.json", snapshot)
            observations.add(1, {"outcome": "holding", "node": settings.node_name})
            logger.info(
                "Successor holding, not qualification",
                extra={
                    "job_id": intent.request.original_job_id,
                    "recovery_id": intent.recovery_id,
                    "succession_id": claim.grant.observation.succession_id,
                    "node": settings.node_name,
                },
            )
        remaining = remaining_budget()
        if remaining <= 0:
            refuse_expired_budget(
                settings, claim, "Successor controller original recovery budget expired"
            )
        stopped.wait(min(1.0, remaining))
