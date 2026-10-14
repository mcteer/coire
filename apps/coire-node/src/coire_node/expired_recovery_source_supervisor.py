"""Fixed fixture supervisor adopts Source children with inherited kernel exclusion.

The support runtime remains immutable. No engine is loaded here. Source node
and guard bytes remain sealed; the supervisor does not edit their old history.
"""

import fcntl
import logging
import os
import platform
import signal
import stat
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psutil
from opentelemetry import metrics, trace

from coire_core.errors import CoireError, UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeColdClosureSupervision,
    UpgradeExpiredRecoveryColdClosure,
    UpgradeExpiredRecoveryColdResolution,
    UpgradeExpiredRecoveryColdResolutionRead,
    UpgradeExpiredRecoveryColdSupervisionIntent,
    UpgradeExpiredRecoveryColdSupervisionReceipt,
)
from coire_core.models.upgrades import NodeAgentProcessIdentity
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.recovery_identity import recovery_kernel_boot_identity
from coire_node.upgrade_guard import (
    NativeGuardTransport,
    _owned_native_process,
    _owned_node_process,
)
from coire_node.upgrade_runtime import verify_prepared_runtime

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.node.source_supervisor")
heartbeat = metrics.get_meter("coire.node.source_supervisor").create_gauge(
    "coire_upgrade_source_supervisor_heartbeat_seconds"
)
required = metrics.get_meter("coire.node.source_supervisor").create_gauge(
    "coire_upgrade_source_supervisor_required"
)


def verify_inherited_fixture_lock(settings: Settings, descriptor: int) -> None:
    path = Path(settings.upgrade_bootstrap_root).parent / "fixture-records/fixture-supervisor.lock"
    try:
        held = os.fstat(descriptor)
        actual = path.stat(follow_symlinks=False)
        if (
            (held.st_dev, held.st_ino) != (actual.st_dev, actual.st_ino)
            or held.st_uid != os.getuid()
            or held.st_mode & 0o077
            or not stat.S_ISREG(held.st_mode)
            or held.st_nlink != 1
        ):
            raise UpgradeUnavailable("Source supervisor has no exact private inherited descriptor")
        try:
            with PrivateUpgradeStore(path.parent, create=False).ownership(path.name):
                raise UpgradeUnavailable("Source supervisor has no continuous kernel exclusion")
        except UpgradeConflict:
            pass
        # An independently opened descriptor cannot reaffirm another owner's
        # flock. The inherited open description can, without an unlock/relock.
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise UpgradeUnavailable(
            "Source supervisor inherited exclusion cannot be verified"
        ) from None


def verify_source_children(
    settings: Settings, intent: UpgradeExpiredRecoveryColdSupervisionIntent
) -> None:
    source = intent.handback.helper.handback.verification.request.smoke.smoke.source
    runtime = source.launch.source.projection.source_selection.runtime
    envs = Path(settings.upgrade_environment_root)
    python = verify_prepared_runtime(envs, envs.parent / "runtimes", runtime)
    guard = _owned_native_process(
        intent.handback.helper.process, python, "coire_node.upgrade_guard"
    )
    node = _owned_node_process(source.process, python)
    if (
        guard is None
        or node is None
        or guard.cmdline() != [str(python), "-I", "-B", "-m", "coire_node.upgrade_guard"]
        or node.cmdline() != [str(python), "-I", "-B", "-m", "coire_node"]
    ):
        raise UpgradeUnavailable("Source supervisor cannot adopt unknown or absent Source children")


@tracer.start_as_current_span("coire.node.source_supervisor_adopt")
def linked_supervision_matches(
    resolution: UpgradeExpiredRecoveryColdResolution,
    receipt: UpgradeExpiredRecoveryColdSupervisionReceipt,
) -> bool:
    return resolution.closure.supervision in (
        receipt,
        UpgradeColdClosureSupervision.from_receipt(receipt),
    )


def run_source_supervisor(settings: Settings, stopped: threading.Event) -> None:
    descriptor = settings.upgrade_recovery_supervision_fd
    if (
        descriptor is None
        or settings.upgrade_recovery_id is None
        or sys.platform != "darwin"
        or platform.machine() != "arm64"
        or platform.node().lower().split(".", 1)[0] != settings.node_name
    ):
        raise UpgradeUnavailable("Source supervisor is not a scoped native Studio process")
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
    run = PrivateUpgradeStore(runs.path(settings.upgrade_recovery_id), create=False)
    intent = run.read("cold.supervision.intent.json", UpgradeExpiredRecoveryColdSupervisionIntent)
    if intent is None:
        raise UpgradeUnavailable("Source supervisor has no retained adoption intent")
    owner = intent.handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
    if owner.supervision != "fixture" or owner.intent.recovery_id != settings.upgrade_recovery_id:
        raise UpgradeConflict("Source supervisor adoption scope differs")
    envs = Path(settings.upgrade_environment_root)
    python = verify_prepared_runtime(envs, envs.parent / "runtimes", owner.runtime)
    process = NodeAgentProcessIdentity(
        pid=os.getpid(),
        create_time_s=psutil.Process(os.getpid()).create_time(),
        environment_sha256=owner.runtime.environment_sha256,
    )
    if (
        Path(sys.prefix).resolve(strict=True)
        != (envs / owner.runtime.environment_id).resolve(strict=True)
        or Path(sys.executable).resolve(strict=True) != python.resolve(strict=True)
        or psutil.Process(os.getpid()).cmdline()
        != [str(python), "-I", "-B", "-m", "coire_node.expired_recovery_source_supervisor"]
        or recovery_kernel_boot_identity(owner.intent.deadline_at) != owner.kernel_boot_identity
        or _owned_native_process(owner.process, python, "coire_node.upgrade_guard") is None
    ):
        raise UpgradeUnavailable("Source supervisor immutable runtime, argv or kernel differs")
    verify_inherited_fixture_lock(settings, descriptor)
    verify_source_children(settings, intent)
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=5.0) as client:
        control = NativeGuardTransport(settings, client)
        if control.accept_cold_recovery_handback_receipt(intent.handback) != intent.handback:
            raise UpgradeUnavailable("Source supervisor has no live accepted handback")
        verify_inherited_fixture_lock(settings, descriptor)
        verify_source_children(settings, intent)
        receipt = UpgradeExpiredRecoveryColdSupervisionReceipt(
            supervision=intent, process=process, kind="fixture", observed_at=datetime.now(UTC)
        )
        with run.ownership("cold.supervision.publish.lock"):
            previous = run.read(
                "cold.supervision.receipt.json", UpgradeExpiredRecoveryColdSupervisionReceipt
            )
            if previous is not None and previous != receipt:
                raise UpgradeConflict("Source supervisor adoption receipt is already retained")
            run.write("cold.supervision.receipt.json", receipt, replace=False)
        logger.info(
            "Source children adopted",
            extra={
                "job_id": owner.original_claim.job_id,
                "recovery_id": owner.intent.recovery_id,
                "user_id": str(owner.intent.accepted_by),
                "node": settings.node_name,
            },
        )
        required.set(1, {"node": settings.node_name})
        # Hold the inherited descriptor across parent close. Until a linked Core
        # terminal record is retained, a missing child refuses; no restart can
        # manufacture fresh serving proof or borrow the expired original epoch.
        while not stopped.is_set():
            verify_inherited_fixture_lock(settings, descriptor)
            resolution = run.read("cold.resolution.json", UpgradeExpiredRecoveryColdResolution)
            if resolution is None and datetime.now(UTC) >= owner.intent.deadline_at:
                closure = run.read("cold.closure.receipt.json", UpgradeExpiredRecoveryColdClosure)
                if closure is None:
                    raise UpgradeUnavailable(
                        "Source supervisor expired before a retained local closure"
                    )
                state = control.cold_resolution_state(
                    owner.intent.recovery_id,
                    UpgradeExpiredRecoveryColdResolutionRead(
                        intent_sha256=owner.intent.sha256(), closure_sha256=closure.sha256()
                    ),
                )
                resolution = state.resolution
                if resolution is None:
                    raise UpgradeUnavailable(
                        "Source supervisor expired before committed Core resolution"
                    )
                run.write("cold.resolution.json", resolution, replace=False)
            if resolution is not None and not linked_supervision_matches(resolution, receipt):
                raise UpgradeConflict("Source supervisor linked terminal owner differs")
            verify_source_children(settings, intent)
            heartbeat.set(time.time(), {"node": settings.node_name})
            stopped.wait(1.0)


def main() -> None:
    from coire_core.logging import StructuredJSONFormatter
    from coire_node.keychain import NODE_TOKEN_ITEM, read_item
    from coire_node.otel import configure_node_telemetry

    handler = logging.StreamHandler()
    handler.setFormatter(StructuredJSONFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    settings = Settings()
    if not settings.node_token.get_secret_value():
        settings.node_token = read_item(NODE_TOKEN_ITEM)
    configure_node_telemetry(
        "source-supervisor",
        settings.otlp_endpoint,
        diagnostics_enabled=settings.diagnostics_enabled,
    )
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    try:
        run_source_supervisor(settings, stopped)
    except (CoireError, OSError, ValueError, psutil.Error):
        logger.error("Source supervision refused")
        raise SystemExit(1) from None
    finally:
        heartbeat.set(0, {"node": settings.node_name})


if __name__ == "__main__":
    main()
