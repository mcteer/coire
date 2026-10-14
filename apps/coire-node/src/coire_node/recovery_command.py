"""Bounded private command capture for fixed, producer-authorized native service actions."""

import logging
import math
import os
import selectors
import signal
import subprocess
import time

import psutil

from coire_core.errors import UpgradeUnavailable
from coire_node.collective_process import _wait_group_stopped

logger = logging.getLogger(__name__)


def capture_owned_command(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
    """Internal argv only, no shell. Reserve cleanup inside original budget.

    Production callers must choose fixed allowlisted service argv; this helper is not a
    remotely callable command runner. Never expose or log captured service output.
    """
    if not math.isfinite(timeout) or timeout <= 0 or not argv or not os.path.isabs(argv[0]):
        raise UpgradeUnavailable("Recovery inspection command budget or executable differs")
    deadline = time.monotonic() + timeout
    reserve = min(0.5, timeout / 2)
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    )
    output = bytearray()
    errors = bytearray()
    born: float | None = None
    try:
        born = psutil.Process(process.pid).create_time()
        assert process.stdout is not None and process.stderr is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, output)
            selector.register(process.stderr, selectors.EVENT_READ, errors)
            while selector.get_map():
                remaining = deadline - time.monotonic() - reserve
                if remaining <= 0:
                    logger.error(
                        "native recovery inspection timed out",
                        extra={
                            "command_type": (
                                os.path.basename(argv[0])
                                if argv[0] in {"/usr/sbin/sysctl", "/bin/launchctl"}
                                else "private-inspection"
                            ),
                            "reason": (
                                f"budget_s={timeout:.6f} "
                                f"elapsed_s={time.monotonic() - deadline + timeout:.6f} "
                                f"exit_code={process.poll()} "
                                f"stdout_bytes={len(output)} stderr_bytes={len(errors)}"
                            ),
                        },
                    )
                    raise UpgradeUnavailable("Recovery inspection command timed out")
                for selected, _ in selector.select(remaining):
                    chunk = os.read(selected.fd, 32768)
                    if not chunk:
                        selector.unregister(selected.fileobj)
                    elif len(output) + len(errors) + len(chunk) > 512 * 1024:
                        raise UpgradeUnavailable("Recovery inspection output exceeds bound")
                    else:
                        selected.data.extend(chunk)
        process.wait(timeout=max(0.001, deadline - time.monotonic() - reserve))
        return subprocess.CompletedProcess(argv, process.returncode, bytes(output), bytes(errors))
    except (OSError, psutil.Error, subprocess.TimeoutExpired):
        raise UpgradeUnavailable("Recovery inspection command cannot be verified") from None
    finally:
        try:
            if process.poll() is None:
                try:
                    live = psutil.Process(process.pid)
                    if (
                        live.create_time() != born
                        or live.uids().real != os.getuid()
                        or os.getpgid(process.pid) != process.pid
                        or live.cmdline() != argv
                    ):
                        raise UpgradeUnavailable("Recovery inspection cleanup identity differs")
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=max(0.001, deadline - time.monotonic()))
                except psutil.NoSuchProcess:
                    process.wait(timeout=max(0.001, deadline - time.monotonic()))
                except (OSError, psutil.Error, subprocess.TimeoutExpired):
                    raise UpgradeUnavailable(
                        "Recovery inspection cleanup remains uncertain"
                    ) from None
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
            _wait_group_stopped(process.pid, deadline)
