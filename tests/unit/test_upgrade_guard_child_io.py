"""Actual CPU child pipes are bounded without forced process termination."""

import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from coire_core.errors import UpgradeUnavailable


@pytest.mark.parametrize("fault", [None, "oversize", "nonzero", "blocked_input"])
def test_child_pipe_exchange_enforces_deadline_and_output_bound(fault: str | None) -> None:
    from coire_node.upgrade_guard import exchange_smoke_child

    program = "import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(b'proof')"
    payload = b"authority"
    duration = 2.0
    if fault == "oversize":
        program = "import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(b'x' * 8192)"
    elif fault == "nonzero":
        program = "import sys; sys.stdin.buffer.read(); sys.exit(1)"
    elif fault == "blocked_input":
        program = "import time; time.sleep(2)"
        payload = b"x" * (512 * 1024)
        duration = 0.1
    process = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", program],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    started = time.monotonic()
    try:
        if fault:
            with pytest.raises(UpgradeUnavailable):
                exchange_smoke_child(
                    process, payload, datetime.now(UTC) + timedelta(seconds=duration)
                )
        else:
            assert (
                exchange_smoke_child(
                    process, payload, datetime.now(UTC) + timedelta(seconds=duration)
                )
                == b"proof"
            )
            assert process.returncode == 0
        if fault == "blocked_input":
            assert time.monotonic() - started < 1
            assert process.poll() is None  # The guard has not killed an uncertain child.
    finally:
        if process.poll() is None:
            process.terminate()  # Test owns this CPU fixture, outside production recovery.
        process.wait(timeout=3)


def test_child_exchange_live_authority_refuses_before_input_delivery() -> None:
    from coire_node.upgrade_guard import exchange_smoke_child

    process = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", "import sys; sys.stdin.buffer.read(); sys.exit(0)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    gate = Mock(side_effect=UpgradeUnavailable("fixture actor revoked"))
    try:
        with pytest.raises(UpgradeUnavailable, match="revoked"):
            exchange_smoke_child(
                process, b"authority", datetime.now(UTC) + timedelta(seconds=2), authority=gate
            )
        gate.assert_called_once()
        assert process.wait(timeout=2) == 0
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=2)
