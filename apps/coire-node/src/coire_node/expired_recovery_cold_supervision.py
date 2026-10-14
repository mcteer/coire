"""Journaled Source fixture supervisor handback with actual descriptor inheritance."""

import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdHandbackReceipt,
    UpgradeExpiredRecoveryColdSupervisionIntent,
    UpgradeExpiredRecoveryColdSupervisionReceipt,
)
from coire_core.settings import Settings
from coire_node.expired_recovery_cold_handback import verify_handback_boundary
from coire_node.expired_recovery_cold_source import stores
from coire_node.expired_recovery_source_supervisor import verify_source_children
from coire_node.recovery_lease import recovery_supervision_descriptor, require_recovery_lease
from coire_node.upgrade_guard import NativeGuardTransport, _owned_native_process
from coire_node.upgrade_runtime import verify_prepared_runtime

tracer = trace.get_tracer("coire.node.cold_recovery_supervision")


def verify_fixture_supervisor(
    settings: Settings, receipt: UpgradeExpiredRecoveryColdSupervisionReceipt
) -> None:
    owner = receipt.supervision.handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
    envs = Path(settings.upgrade_environment_root)
    python = verify_prepared_runtime(envs, envs.parent / "runtimes", owner.runtime)
    child = _owned_native_process(
        receipt.process, python, "coire_node.expired_recovery_source_supervisor"
    )
    lock = Path(settings.upgrade_bootstrap_root).parent / "fixture-records/fixture-supervisor.lock"
    if (
        receipt.kind != "fixture"
        or child is None
        or child.cmdline()
        != [str(python), "-I", "-B", "-m", "coire_node.expired_recovery_source_supervisor"]
        or not any(Path(record.path) == lock for record in child.open_files())
    ):
        raise UpgradeUnavailable("Source fixture supervisor has no exact live inherited ownership")
    verify_source_children(settings, receipt.supervision)


@tracer.start_as_current_span("coire.node.cold_recovery_supervise_source")
def supervise_cold_source(
    settings: Settings,
    handback: UpgradeExpiredRecoveryColdHandbackReceipt,
    control: NativeGuardTransport,
) -> UpgradeExpiredRecoveryColdSupervisionReceipt:
    owner = handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
    if owner.supervision != "fixture":
        raise UpgradeUnavailable(
            "Managed Source supervision requires verified launchd registration"
        )
    bootstrap, run, _ = stores(settings, owner)
    require_recovery_lease(bootstrap, owner.sha256())
    with run.ownership("cold.supervision.lock"):
        previous = run.read(
            "cold.supervision.receipt.json", UpgradeExpiredRecoveryColdSupervisionReceipt
        )
        if previous is not None:
            if previous.supervision.handback != handback:
                raise UpgradeConflict("Source fixture supervisor changes retained handback")
            if control.accept_cold_recovery_handback_receipt(handback) != handback:
                raise UpgradeUnavailable("Source fixture supervisor replay has no live handback")
            verify_fixture_supervisor(settings, previous)
            return previous
        verify_handback_boundary(settings, handback.helper.handback, control)
        if control.accept_cold_recovery_handback_receipt(handback) != handback:
            raise UpgradeUnavailable("Source supervisor handback acknowledgment differs")
        intent = run.read(
            "cold.supervision.intent.json", UpgradeExpiredRecoveryColdSupervisionIntent
        )
        if intent is None:
            intent = UpgradeExpiredRecoveryColdSupervisionIntent(
                handback=handback, started_at=datetime.now(UTC)
            )
            run.write("cold.supervision.intent.json", intent, replace=False)
        if intent.handback != handback:
            raise UpgradeConflict("Source fixture supervisor intent differs")
        lock = (
            Path(settings.upgrade_bootstrap_root).parent / "fixture-records/fixture-supervisor.lock"
        )
        descriptor = recovery_supervision_descriptor(bootstrap, owner.sha256(), lock)
        marker = run.read(
            "cold.supervision.launch.json", UpgradeExpiredRecoveryColdSupervisionIntent
        )
        if marker is None:
            run.write("cold.supervision.launch.json", intent, replace=False)
            verify_handback_boundary(settings, handback.helper.handback, control)
            descriptor = recovery_supervision_descriptor(bootstrap, owner.sha256(), lock)
            envs = Path(settings.upgrade_environment_root)
            python = verify_prepared_runtime(envs, envs.parent / "runtimes", owner.runtime)
            environment = dict(os.environ)
            for key in (
                "PYTHONPATH",
                "PYTHONHOME",
                "DYLD_INSERT_LIBRARIES",
                "LD_PRELOAD",
                "UPGRADE_RECOVERY_SUCCESSION_ID",
                "UPGRADE_RECOVERY_SUCCESSION_MODE",
            ):
                environment.pop(key, None)
            environment.update(
                {
                    "NODE_NAME": settings.node_name,
                    "NODE_STATE_DIR": settings.node_state_dir,
                    "NODE_STORE_DIR": settings.node_store_dir,
                    "UPGRADE_ENVIRONMENT_ROOT": settings.upgrade_environment_root,
                    "UPGRADE_BOOTSTRAP_ROOT": settings.upgrade_bootstrap_root,
                    "UPGRADE_RECOVERY_ID": owner.intent.recovery_id,
                    "UPGRADE_RECOVERY_SUPERVISION_FD": str(descriptor),
                    "UPGRADES_ENABLED": "true",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONNOUSERSITE": "1",
                    "HF_HUB_OFFLINE": "1",
                    "TRANSFORMERS_OFFLINE": "1",
                }
            )
            if control.accept_cold_recovery_handback_receipt(handback) != handback:
                raise UpgradeUnavailable("Source supervisor authority changed before spawn")
            require_recovery_lease(bootstrap, owner.sha256())
            subprocess.Popen(
                [str(python), "-I", "-B", "-m", "coire_node.expired_recovery_source_supervisor"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=(descriptor,),
                start_new_session=True,
                env=environment,
            )
        elif marker != intent:
            raise UpgradeConflict("Source fixture supervisor launch marker differs")
        # Lost responses never launch again. Only the fixed child's immutable
        # acknowledgement and actual inherited descriptor can complete handback.
        deadline = min(owner.intent.deadline_at, intent.started_at + timedelta(seconds=30))
        deadline_tick = time.monotonic() + max(0, (deadline - datetime.now(UTC)).total_seconds())
        while datetime.now(UTC) < deadline and time.monotonic() < deadline_tick:
            require_recovery_lease(bootstrap, owner.sha256())
            if control.accept_cold_recovery_handback_receipt(handback) != handback:
                raise UpgradeUnavailable("Source supervisor acknowledgment authority changed")
            receipt = run.read(
                "cold.supervision.receipt.json", UpgradeExpiredRecoveryColdSupervisionReceipt
            )
            if receipt is not None:
                if receipt.supervision != intent:
                    raise UpgradeConflict("Source fixture supervisor acknowledgment changes intent")
                verify_fixture_supervisor(settings, receipt)
                return receipt
            time.sleep(min(0.1, max(0, deadline_tick - time.monotonic())))
        raise UpgradeUnavailable("Source fixture supervisor acknowledgment is unresolved")
