"""Observe uncertain managed retirement without reexecuting any stop command."""

import logging
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import metrics, trace

from coire_core.errors import CoireError, UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryLegacyStopIntent,
    UpgradeExpiredRecoveryLegacyStopReceipt,
    UpgradeExpiredRecoveryLegacyStopReconciliation,
    UpgradeExpiredRecoveryTransferConsumption,
)
from coire_core.models.upgrades import PreparedNativeRuntime
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_definition import read_legacy_definition
from coire_node.expired_recovery_launchd import fixed_launchctl_probe
from coire_node.expired_recovery_preflight import verify_local_successor_identity
from coire_node.expired_recovery_stop import source_identity
from coire_node.recovery_identity import verify_retired_identity
from coire_node.upgrade_guard import NativeGuardTransport

tracer = trace.get_tracer("coire.node.recovery_stop_reconciliation")
logger = logging.getLogger(__name__)
reconciliation_events = metrics.get_meter("coire.node.recovery_stop_reconciliation").create_counter(
    "coire_upgrade_recovery_stop_reconciliation_total",
    description="Observed managed retirement attempts and refusals; never command success",
)


def ensure_initial_retirement(
    settings: Settings,
    receipt: UpgradeExpiredRecoveryTransferConsumption,
    runtime: PreparedNativeRuntime,
    control: NativeGuardTransport,
    *,
    stopped: threading.Event | None = None,
) -> UpgradeExpiredRecoveryLegacyStopReceipt | UpgradeExpiredRecoveryLegacyStopReconciliation:
    """Select by canonical durable evidence, never by catching a stop failure.

    Selection exclusion is released before entering independently locked producers.
    A racing new intent causes the command producer to refuse, never blind retry.
    Existing proof remains historical; later ownership repeats all physical gates.
    """
    from coire_node.expired_recovery_effect_journal import retained_legacy_retirement_proof
    from coire_node.expired_recovery_stop import stop_legacy_guard

    if control.verify_recovery_consumption(receipt).sha256() != receipt.sha256():
        raise UpgradeUnavailable("Initial retirement consumption differs")
    if stopped is not None and stopped.is_set():
        raise UpgradeUnavailable("Initial retirement cancelled after Core probe")
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    with bootstrap.ownership("recovery-transfer.lock"):
        runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
        run = PrivateUpgradeStore(runs.path(receipt.transfer.intent.recovery_id), create=False)
        previous = retained_legacy_retirement_proof(run, receipt)
        if previous is not None:
            return previous
        uncertain = (
            run.read("legacy-stop.intent.json", UpgradeExpiredRecoveryLegacyStopIntent) is not None
        )
    if stopped is not None and stopped.is_set():
        raise UpgradeUnavailable("Initial retirement cancelled before producer dispatch")
    if uncertain:
        return reconcile_legacy_stop(settings, receipt, runtime, control, stopped=stopped)
    return stop_legacy_guard(settings, receipt, runtime, control, stopped=stopped)


def reconcile_legacy_stop(
    settings: Settings,
    receipt: UpgradeExpiredRecoveryTransferConsumption,
    runtime: PreparedNativeRuntime,
    control: NativeGuardTransport,
    *,
    stopped: threading.Event | None = None,
) -> UpgradeExpiredRecoveryLegacyStopReconciliation:
    """Observable wrapper; no raw service definitions/credentials or engine traces."""
    fields = {
        "job_id": receipt.transfer.intent.request.original_job_id,
        "recovery_id": receipt.transfer.intent.recovery_id,
        "generation": receipt.transfer.intent.original_plan.operation.generation,
    }
    try:
        proof = _reconcile_legacy_stop(settings, receipt, runtime, control, stopped=stopped)
    except CoireError:
        reconciliation_events.add(1, {"outcome": "refused"})
        logger.warning("native retirement reconciliation refused", extra=fields)
        raise
    reconciliation_events.add(1, {"outcome": "observed"})
    logger.info("native retirement reconciliation history retained", extra=fields)
    return proof


def _reconcile_legacy_stop(
    settings: Settings,
    receipt: UpgradeExpiredRecoveryTransferConsumption,
    runtime: PreparedNativeRuntime,
    control: NativeGuardTransport,
    *,
    stopped: threading.Event | None = None,
) -> UpgradeExpiredRecoveryLegacyStopReconciliation:
    """Same initial consumer/window only; no succession or deadline renewal.

    Original effect and acknowledged receipt remain untouched. Reconciliation is
    distinct observed retirement, not an acknowledgement that bootout ran and not
    actual ownership/serving/pair/data/complete collective cleanup qualification.
    """
    with tracer.start_as_current_span("coire.node.recovery_stop_reconcile"):
        deadline = min(receipt.transfer.expires_at, receipt.transfer.intent.deadline_at)
        deadline_tick = time.monotonic() + max(
            0.0, min(30.0, (deadline - datetime.now(UTC)).total_seconds())
        )

        def require_budget() -> None:
            if datetime.now(UTC) >= deadline or time.monotonic() >= deadline_tick:
                raise UpgradeUnavailable("Retirement reconciliation captured budget expired")

        require_budget()
        if control.verify_recovery_consumption(receipt).sha256() != receipt.sha256():
            raise UpgradeUnavailable("Retirement reconciliation Core scope differs")
        require_budget()
        if stopped is not None and stopped.is_set():
            raise UpgradeUnavailable("Retirement reconciliation cancelled after Core probe")
        bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
        with bootstrap.ownership("recovery-transfer.lock"):
            runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
            run = PrivateUpgradeStore(runs.path(receipt.transfer.intent.recovery_id), create=False)
            if run.read("consumption.json", UpgradeExpiredRecoveryTransferConsumption) != receipt:
                raise UpgradeConflict("Retirement reconciliation retained consumption differs")
            if (
                run.read("legacy-stop.receipt.json", UpgradeExpiredRecoveryLegacyStopReceipt)
                is not None
            ):
                raise UpgradeConflict(
                    "Acknowledged retirement cannot become observed reconciliation"
                )
            effect = run.read("legacy-stop.intent.json", UpgradeExpiredRecoveryLegacyStopIntent)
            if effect is None or effect.service is None or effect.preserved_source is None:
                raise UpgradeUnavailable("Retirement reconciliation lacks bound managed intent")
            candidate = UpgradeExpiredRecoveryLegacyStopReconciliation(
                consumption=receipt,
                effect=effect,
                preserved_source=effect.preserved_source,
                observed_at=datetime.now(UTC),
                service_unloaded=True,
                legacy_identity_retired=True,
            )
            # DTO validates canonical scope only; actual positive facts below are mandatory.
            for _ in range(2):
                if control.verify_recovery_consumption(receipt).sha256() != receipt.sha256():
                    raise UpgradeUnavailable("Retirement reconciliation authority changed")
                require_budget()
                if stopped is not None and stopped.is_set():
                    raise UpgradeUnavailable("Retirement reconciliation cancelled before sampling")
                verify_local_successor_identity(settings, receipt, runtime)
                if (
                    read_legacy_definition(settings, receipt.transfer.legacy_guard, effect.service)
                    != effect.definition_sha256
                ):
                    raise UpgradeUnavailable("Retirement reconciliation definition changed")
                if (
                    fixed_launchctl_probe(receipt.transfer.expires_at)(effect.service.target)
                    is not None
                ):
                    raise UpgradeUnavailable("Retirement reconciliation service remains loaded")
                verify_retired_identity(receipt.transfer.legacy_guard.process)
                if source_identity(settings, receipt) != effect.preserved_source:
                    raise UpgradeUnavailable("Retirement reconciliation Source changed")
            if control.verify_recovery_consumption(receipt).sha256() != receipt.sha256():
                raise UpgradeUnavailable(
                    "Retirement reconciliation authority changed before publication"
                )
            require_budget()
            if stopped is not None and stopped.is_set():
                raise UpgradeUnavailable("Retirement reconciliation cancelled before publication")
            observed = candidate.model_copy(update={"observed_at": datetime.now(UTC)})
            observed = UpgradeExpiredRecoveryLegacyStopReconciliation.model_validate_json(
                observed.model_dump_json()
            )
            previous = run.read(
                "legacy-stop.reconciliation.json", UpgradeExpiredRecoveryLegacyStopReconciliation
            )
            if previous is not None:
                if (
                    previous.consumption != receipt
                    or previous.effect != effect
                    or previous.preserved_source != observed.preserved_source
                ):
                    raise UpgradeConflict("Retained retirement reconciliation differs")
                return previous  # Immutable history; does not renew observed epoch.
            run.write("legacy-stop.reconciliation.json", observed, replace=False)
            return observed
