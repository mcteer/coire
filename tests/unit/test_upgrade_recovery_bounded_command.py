"""Read-only launchctl capture primitive owns and cleans its private child group."""

import sys
from unittest.mock import Mock

import psutil
import pytest

from coire_core.errors import UpgradeUnavailable


@pytest.mark.parametrize("budget", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_command_budget_refuses_before_spawn(monkeypatch, budget):
    from coire_node import recovery_command as module

    spawn = Mock(side_effect=AssertionError("nonfinite budget reached process spawn"))
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    with pytest.raises(UpgradeUnavailable):
        module.capture_owned_command([sys.executable, "-c", "pass"], timeout=budget)
    spawn.assert_not_called()


@pytest.mark.parametrize("fault", [None, "oversize", "timeout"])
def test_bounded_probe_capture_cleans_private_process(tmp_path, fault):
    from coire_node.recovery_command import capture_owned_command

    record = tmp_path / "pid"
    script = f"import os,time; from pathlib import Path; Path({str(record)!r}).write_text(str(os.getpid())); "
    script += {None: "print('ok')", "oversize": "print('x'*600000)", "timeout": "time.sleep(30)"}[
        fault
    ]
    if fault:
        with pytest.raises(UpgradeUnavailable):
            capture_owned_command([sys.executable, "-I", "-S", "-c", script], timeout=1)
    else:
        result = capture_owned_command([sys.executable, "-I", "-S", "-c", script], timeout=1)
        assert result.returncode == 0 and result.stdout == b"ok\n"
    if record.exists():
        assert not psutil.pid_exists(int(record.read_text()))


def test_timeout_diagnostics_do_not_emit_private_argv(tmp_path, caplog):
    from coire_core.logging import StructuredJSONFormatter
    from coire_node.recovery_command import capture_owned_command

    private_argument = "private-recovery-token-must-not-be-logged"
    with pytest.raises(UpgradeUnavailable, match="timed out"):
        capture_owned_command(
            [sys.executable, "-c", "import time; time.sleep(30)", private_argument],
            timeout=1,
        )
    record = next(
        entry for entry in caplog.records if entry.message == "native recovery inspection timed out"
    )
    rendered = StructuredJSONFormatter().format(record)
    assert private_argument not in rendered
    assert sys.executable not in rendered
    assert '"command_type":"private-inspection"' in rendered
    assert "budget_s=1.000000" in rendered
    assert "elapsed_s=" in rendered
    assert "stdout_bytes=0 stderr_bytes=0" in rendered
