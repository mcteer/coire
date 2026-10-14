"""Fixed managed legacy stop; original journals/fences and Source stay retained."""

import logging
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import metrics, trace

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryLegacyStopReceipt,
    UpgradeExpiredRecoveryTransferConsumption,
)
from coire_core.models.upgrades import NativeNodeProcess, PreparedNativeRuntime
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_definition import read_legacy_definition
from coire_node.expired_recovery_effect_journal import begin_legacy_stop, retained_legacy_stop_proof
from coire_node.expired_recovery_launchd import fixed_launchctl_probe, observe_legacy_guard_service
from coire_node.expired_recovery_preflight import verify_local_transfer_preflight
from coire_node.recovery_command import capture_owned_command
from coire_node.recovery_identity import verify_retired_identity
from coire_node.upgrade_guard import NativeGuardTransport, _owned_native_process

tracer = trace.get_tracer("coire.node.recovery_legacy_stop")
logger = logging.getLogger(__name__)
stop_budget_events = metrics.get_meter("coire.node.recovery_legacy_stop").create_counter(
    "coire_upgrade_recovery_stop_budget_total",
    description="Captured managed-stop budget refusals; never command acknowledgement",
)


def source_identity(
    settings: Settings, receipt: UpgradeExpiredRecoveryTransferConsumption
) -> NativeNodeProcess:
    source = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades", create=False).read(
        "node-process.json", NativeNodeProcess
    )
    plan = receipt.transfer.intent.original_plan
    if (
        source is None
        or source.selected != receipt.transfer.legacy_guard.selected
        or source.selected.release_sha256 != plan.source.manifest_sha256
    ):
        raise UpgradeUnavailable("Source selection differs before legacy stop")
    python = (
        Path(settings.upgrade_environment_root)
        / source.selected.runtime.environment_id
        / "bin/python3"
    )
    if _owned_native_process(source.process, python, "coire_node") is None:
        raise UpgradeUnavailable("Source process is absent or uncertain")
    return source


def stop_legacy_guard(
    settings: Settings,
    receipt: UpgradeExpiredRecoveryTransferConsumption,
    runtime: PreparedNativeRuntime,
    control: NativeGuardTransport,
    *,
    stopped: threading.Event | None = None,
) -> UpgradeExpiredRecoveryLegacyStopReceipt:
    """No privilege escalation/shell; unavailable bootout keeps recorded uncertainty.

    Existing intent never retries bootout. Separate reconciliation and complete
    physical cleanup/qualification/ownership proof are required before release.
    """
    deadline = min(receipt.transfer.expires_at, receipt.transfer.intent.deadline_at)
    deadline_tick = time.monotonic() + max(
        0.0, min(30.0, (deadline - datetime.now(UTC)).total_seconds())
    )

    def require_budget() -> None:
        with tracer.start_as_current_span("coire.node.recovery_legacy_stop_budget"):
            if datetime.now(UTC) >= deadline or time.monotonic() >= deadline_tick:
                stop_budget_events.add(1, {"outcome": "refused"})
                logger.warning(
                    "native legacy stop captured budget refused",
                    extra={
                        "job_id": receipt.transfer.intent.request.original_job_id,
                        "recovery_id": receipt.transfer.intent.recovery_id,
                        "generation": receipt.transfer.intent.original_plan.operation.generation,
                    },
                )
                raise UpgradeUnavailable("Legacy stop captured budget expired")

    require_budget()
    receipt = control.verify_recovery_consumption(receipt)
    require_budget()
    if stopped is not None and stopped.is_set():
        raise UpgradeUnavailable("Legacy stop cancelled after Core probe")
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    with bootstrap.ownership("recovery-transfer.lock"):
        runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
        run = PrivateUpgradeStore(runs.path(receipt.transfer.intent.recovery_id), create=False)
        previous = retained_legacy_stop_proof(run, receipt)
        if previous is not None:
            return previous  # Historical result, never fresh qualification/ownership.
        if run.path("legacy-stop.intent.json").exists():
            raise UpgradeUnavailable(
                "Interrupted legacy stop requires reconciliation, not reexecution"
            )
        verify_local_transfer_preflight(settings, receipt, runtime)
        source = source_identity(settings, receipt)
        legacy = receipt.transfer.legacy_guard
        service = observe_legacy_guard_service(
            legacy, os.getuid(), fixed_launchctl_probe(receipt.transfer.expires_at)
        )
        definition = read_legacy_definition(settings, legacy, service)
        verify_local_transfer_preflight(settings, receipt, runtime)
        receipt = control.verify_recovery_consumption(receipt)
        if stopped is not None and stopped.is_set():
            raise UpgradeUnavailable("Legacy stop cancelled before intent")
        require_budget()
        effect, fresh = begin_legacy_stop(
            run,
            receipt,
            datetime.now(UTC),
            service=service,
            definition_sha256=definition,
            preserved_source=source,
        )
        if not fresh or effect.service is None or effect.definition_sha256 is None:
            raise UpgradeUnavailable("Legacy stop has no fresh bound managed effect")
        control.verify_recovery_consumption(receipt)
        if read_legacy_definition(settings, legacy, service) != definition:
            raise UpgradeUnavailable("Legacy service definition changed before stop")
        current_service = observe_legacy_guard_service(
            legacy, os.getuid(), fixed_launchctl_probe(receipt.transfer.expires_at)
        )
        if (current_service.target, current_service.process, current_service.owner_uid) != (
            service.target,
            service.process,
            service.owner_uid,
        ):
            raise UpgradeUnavailable("Legacy managed owner changed before stop")
        verify_local_transfer_preflight(settings, receipt, runtime)
        if source_identity(settings, receipt) != source:
            raise UpgradeUnavailable("Source identity changed before legacy stop")
        control.verify_recovery_consumption(receipt)
        if stopped is not None and stopped.is_set():
            raise UpgradeUnavailable("Legacy stop cancelled before bootout")
        require_budget()
        remaining = min(
            (deadline - datetime.now(UTC)).total_seconds(), deadline_tick - time.monotonic()
        )
        result = capture_owned_command(
            ["/bin/launchctl", "bootout", service.target], timeout=min(5.0, remaining / 2)
        )
        if result.returncode != 0:
            raise UpgradeUnavailable("Legacy service stop was not acknowledged")
        if fixed_launchctl_probe(receipt.transfer.expires_at)(service.target) is not None:
            raise UpgradeUnavailable("Legacy service remains loaded after stop")
        verify_retired_identity(legacy.process)
        if source_identity(settings, receipt) != source:
            raise UpgradeUnavailable("Source identity changed during legacy stop")
        require_budget()
        proof = UpgradeExpiredRecoveryLegacyStopReceipt(
            effect=effect, preserved_source=source, stopped_at=datetime.now(UTC)
        )
        run.write("legacy-stop.receipt.json", proof, replace=False)
        return proof
