"""Bounded collective launch with explicitly owned local process-group cleanup."""

import os
import signal
import subprocess
import time
from collections.abc import Callable

import psutil
from opentelemetry import trace

from coire_core.errors import CoireError, UpgradeUnavailable

tracer = trace.get_tracer("coire.node.collective_process")


def _wait_group_stopped(group_id: int, deadline: float) -> None:
    while True:
        active = False
        try:
            for index, member in enumerate(psutil.process_iter()):
                if index >= 4096:
                    raise UpgradeUnavailable("Collective group census exceeds its bound")
                try:
                    if (
                        os.getpgid(member.pid) == group_id
                        and member.status() != psutil.STATUS_ZOMBIE
                    ):
                        active = True
                except (ProcessLookupError, psutil.NoSuchProcess):
                    continue
        except (OSError, psutil.Error):
            raise UpgradeUnavailable("Collective group cleanup cannot be observed") from None
        if not active:
            return
        if time.monotonic() >= deadline:
            raise UpgradeUnavailable("Collective group still owns active descendants")
        time.sleep(min(0.01, max(0, deadline - time.monotonic())))


@tracer.start_as_current_span("coire.node.collective.launch", record_exception=False)
def run_collective_launcher(
    argv: list[str],
    *,
    timeout: float,
    env: dict[str, str],
    authority: Callable[[], None] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Reserve cleanup inside the same budget; never signal an unverified group.

    A failed local launch is not remote cleanup or paired qualification. The
    independent peer census and durable scope still gate maintenance release.
    """
    if timeout <= 0 or not argv or not os.path.isabs(argv[0]):
        raise ValueError("Collective requires an explicit executable and positive budget")
    deadline = time.monotonic() + timeout
    cleanup_budget = min(2.0, timeout / 2)
    if authority is not None:
        authority()
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    try:
        identity = psutil.Process(process.pid)
        born = identity.create_time()
        while True:
            available = deadline - time.monotonic() - cleanup_budget
            if available <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            if authority is not None:
                try:
                    authority()
                except CoireError:
                    # Cleanup follows the same positively owned group path.
                    # Return its real interrupted exit, never qualification.
                    raise subprocess.TimeoutExpired(argv, timeout) from None
            try:
                output, _ = process.communicate(
                    timeout=available if authority is None else min(0.25, available)
                )
                break
            except subprocess.TimeoutExpired:
                if authority is None or time.monotonic() >= deadline - cleanup_budget:
                    raise
    except subprocess.TimeoutExpired:
        try:
            live = psutil.Process(process.pid)
            if (
                live.create_time() != born
                or live.uids().real != os.getuid()
                or process.poll() is not None
                or os.getpgid(process.pid) != process.pid
                or live.cmdline() != argv
            ):
                raise UpgradeUnavailable("Collective timeout group identity is unverified")
            # The still-owned session leader pins the group id. Signal its
            # inherited SSH children as well, not just the MLX launcher.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UpgradeUnavailable("Collective timeout cleanup exceeded its original budget")
            os.killpg(process.pid, signal.SIGTERM)
            output, _ = process.communicate(timeout=remaining)
        except (psutil.Error, OSError, subprocess.TimeoutExpired):
            raise UpgradeUnavailable("Collective timeout cleanup remains unresolved") from None
        _wait_group_stopped(process.pid, deadline)
        return subprocess.CompletedProcess(argv, -signal.SIGTERM, output)
    except psutil.Error:
        raise UpgradeUnavailable("Collective launcher identity is unavailable") from None
    _wait_group_stopped(process.pid, deadline)
    return subprocess.CompletedProcess(argv, process.returncode, output)
