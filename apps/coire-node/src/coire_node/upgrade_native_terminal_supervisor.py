"""Adopt verified fixture children and retain the ordinary native restart policy."""

import logging
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

import psutil
from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_native_terminal import (
    UpgradeNativeTerminalSupervisorIntent,
    UpgradeNativeTerminalSupervisorReceipt,
)
from coire_core.models.upgrades import (
    NativeGuardStatus,
    NativeNodeProcess,
    NodeAgentProcessIdentity,
    PreparedNativeRuntime,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_source_supervisor import (
    heartbeat,
    required,
    verify_inherited_fixture_lock,
)
from coire_node.upgrade_guard import _owned_native_process, _owned_node_process
from coire_node.upgrade_runtime import verify_prepared_runtime

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.node.native_terminal_supervisor")


def private_output(path: Path) -> BinaryIO:
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        os.close(descriptor)
        raise UpgradeUnavailable("Fixture supervisor output is not private and owned")
    return os.fdopen(descriptor, "ab")


def current_children(settings: Settings) -> tuple[NativeGuardStatus, NativeNodeProcess]:
    root = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    state = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades", create=False)
    guard = root.read("status.json", NativeGuardStatus)
    node = state.read("node-process.json", NativeNodeProcess)
    if (
        guard is None
        or node is None
        or guard.node != settings.node_name
        or node.selected.node != settings.node_name
    ):
        raise UpgradeUnavailable("Fixture adoption current child records are absent or foreign")
    return guard, node


def stable_current_children(settings: Settings) -> tuple[NativeGuardStatus, NativeNodeProcess]:
    """An atomic heartbeat replacement may retire the opened inode mid-read."""
    deadline = time.monotonic() + 2
    while True:
        try:
            return current_children(settings)
        except UpgradeUnavailable:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


@tracer.start_as_current_span("coire.node.native_terminal_supervisor.adopt", record_exception=False)
def adopt_fixture(settings: Settings, stopped: threading.Event) -> None:
    recovery_id = settings.upgrade_recovery_id
    descriptor = settings.upgrade_recovery_supervision_fd
    if recovery_id is None or descriptor is None:
        raise UpgradeUnavailable("Fixture adoption has no fixed recovery or inherited descriptor")
    root = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    run = PrivateUpgradeStore(root.root / "expired-recovery" / recovery_id, create=False)
    intent = run.read(
        "native-terminal.supervisor.intent.json", UpgradeNativeTerminalSupervisorIntent
    )
    if intent is None:
        raise UpgradeUnavailable("Fixture adoption has no immutable producer intent")
    envs = Path(settings.upgrade_environment_root)
    python = verify_prepared_runtime(envs, envs.parent / "runtimes", intent.runtime)
    if Path(sys.prefix).resolve(strict=True) != (envs / intent.runtime.environment_id).resolve(
        strict=True
    ) or Path(sys.executable).resolve(strict=True) != python.resolve(strict=True):
        raise UpgradeUnavailable("Fixture adoption interpreter differs")
    verify_inherited_fixture_lock(settings, descriptor)
    process = NodeAgentProcessIdentity(
        pid=os.getpid(),
        create_time_s=psutil.Process(os.getpid()).create_time(),
        environment_sha256=intent.runtime.environment_sha256,
    )
    guard, node = current_children(settings)
    closure = intent.grant.resolution.closure
    if node != closure.node or guard.process != closure.handback.helper.process:
        raise UpgradeConflict("Fixture adoption children differ from canonical handback")
    for identity, module, runtime in [
        (guard.process, "coire_node.upgrade_guard", guard.bootstrap_runtime),
        (node.process, "coire_node", node.selected.runtime),
    ]:
        child_python = verify_prepared_runtime(envs, envs.parent / "runtimes", runtime)
        if _owned_native_process(identity, child_python, module) is None:
            raise UpgradeUnavailable("Fixture adoption child has no positive live identity")
    receipt = UpgradeNativeTerminalSupervisorReceipt(
        intent=intent, process=process, observed_at=datetime.now(UTC)
    )
    if (
        run.read("native-terminal.supervisor.receipt.json", UpgradeNativeTerminalSupervisorReceipt)
        is not None
    ):
        raise UpgradeConflict("Fixture adoption receipt already exists")
    run.write("native-terminal.supervisor.receipt.json", receipt, replace=False)
    required.set(1, {"node": settings.node_name})
    logger.info(
        "native fixture supervision adopted",
        extra={
            "job_id": closure.node.selected.job_id,
            "node": settings.node_name,
            "recovery_id": recovery_id,
            "pid": os.getpid(),
        },
    )
    outputs = []
    launched = []
    pending: dict[str, subprocess.Popen[bytes]] = {}
    child_environment = dict(os.environ)
    for key in (
        "UPGRADE_RECOVERY_ID",
        "UPGRADE_RECOVERY_SUPERVISION_FD",
        "UPGRADE_RECOVERY_CORE_API_PORT",
        "UPGRADE_RECOVERY_SUCCESSION_ID",
        "UPGRADE_RECOVERY_SUCCESSION_MODE",
    ):
        child_environment.pop(key, None)
    checked: dict[str, Path] = {}

    def checked_python(runtime: PreparedNativeRuntime) -> Path:
        key = runtime.environment_sha256
        if key not in checked:
            checked[key] = verify_prepared_runtime(envs, envs.parent / "runtimes", runtime)
        return checked[key]

    # Source bytes remain unchanged. After ordinary guard selection, this fixture
    # daemon provides the same node/guard KeepAlive behavior as the normal owner.
    while not stopped.wait(0.2):
        verify_inherited_fixture_lock(settings, descriptor)
        heartbeat.set(time.time(), {"node": settings.node_name})
        guard, node = stable_current_children(settings)
        guard_python = checked_python(guard.bootstrap_runtime)
        guard_child = _owned_native_process(guard.process, guard_python, "coire_node.upgrade_guard")
        if guard_child is None:
            waiting = pending.get("guard")
            if waiting is not None and waiting.poll() is None:
                continue
            bootstrap = root.read("runtime.json", PreparedNativeRuntime)
            if bootstrap is None:
                raise UpgradeUnavailable("Fixture restart bootstrap is absent")
            guard_python = checked_python(bootstrap)
            with root.ownership("lifetime.lock"):
                pass  # Positive absence of another guard before starting its fixed argv.
            output = private_output(run.root / "native-terminal.guard.log")
            outputs.append(output)
            launched.append(
                subprocess.Popen(
                    [str(guard_python), "-I", "-B", "-m", "coire_node.upgrade_guard"],
                    env=child_environment,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                )
            )
            pending["guard"] = launched[-1]
            stopped.wait(1)
            continue
        node_python = checked_python(node.selected.runtime)
        if _owned_node_process(node.process, node_python) is None:
            waiting = pending.get("node")
            if waiting is not None and waiting.poll() is None:
                continue
            current = (envs / "current").resolve(strict=True)
            if current.parent != envs.resolve(strict=True):
                raise UpgradeUnavailable("Fixture selected node escapes environment root")
            if (
                guard.ready
                and guard.selected is not None
                and guard.selected.runtime.environment_id == current.name
                and 0 <= (datetime.now(UTC) - guard.observed_at).total_seconds() <= 5
            ):
                selected_python = checked_python(guard.selected.runtime)
                output = private_output(run.root / "native-terminal.node.log")
                outputs.append(output)
                launched.append(
                    subprocess.Popen(
                        [str(selected_python), "-I", "-B", "-m", "coire_node"],
                        env=child_environment,
                        stdin=subprocess.DEVNULL,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                    )
                )
                pending["node"] = launched[-1]
                stopped.wait(1)
    # Shutdown relinquishes only supervision. Accepted guard effects own process
    # retirement; this monitor never kills a running node or inference engine.
    for output in outputs:
        output.close()


def main() -> None:
    from coire_core.logging import StructuredJSONFormatter
    from coire_node.otel import configure_node_telemetry

    settings = Settings()
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredJSONFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    configure_node_telemetry(
        "native-terminal-supervisor",
        settings.otlp_endpoint,
        diagnostics_enabled=settings.diagnostics_enabled,
    )
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    try:
        adopt_fixture(settings, stopped)
    finally:
        heartbeat.set(0, {"node": settings.node_name})
        if settings.upgrade_recovery_supervision_fd is not None:
            os.close(settings.upgrade_recovery_supervision_fd)


if __name__ == "__main__":
    main()
