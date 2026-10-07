"""Owned child exit races never become live PID reuse or false group-death proof."""

from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from coire_node.training.supervisor import NativeProcesses


@pytest.mark.parametrize("group_remains", [False, True])
def test_owned_child_reaped_between_poll_and_argv_observation(
    monkeypatch: pytest.MonkeyPatch,
    group_remains: bool,
) -> None:
    processes = NativeProcesses()
    child = Mock()
    child.poll.side_effect = [None, 0]
    processes.children[123] = child
    process = SimpleNamespace(
        create_time=lambda: 10.0, cmdline=lambda: [], status=lambda: psutil.STATUS_ZOMBIE
    )
    monkeypatch.setattr("coire_node.training.supervisor.psutil.Process", lambda _: process)
    monkeypatch.setattr("coire_node.training.supervisor.os.getpgid", lambda _: 123)
    monkeypatch.setattr(
        "coire_node.training.supervisor.psutil.process_iter",
        lambda: [SimpleNamespace(pid=124)] if group_remains else [],
    )
    assert processes.observe(["python", "-m", "coire_node.training.worker"], 123, 10.0) == (
        "unknown" if group_remains else "stopped"
    )
    assert child.poll.call_count == 2


@pytest.mark.parametrize("reused", [False, True])
def test_unowned_live_or_reused_pid_retains_unknown_ownership(
    monkeypatch: pytest.MonkeyPatch,
    reused: bool,
) -> None:
    processes = NativeProcesses()
    child = Mock()
    child.poll.return_value = None
    processes.children[123] = child
    process = SimpleNamespace(
        create_time=lambda: 11.0 if reused else 10.0,
        cmdline=lambda: ["unrelated"],
        status=lambda: psutil.STATUS_RUNNING,
    )
    monkeypatch.setattr("coire_node.training.supervisor.psutil.Process", lambda _: process)
    monkeypatch.setattr("coire_node.training.supervisor.os.getpgid", lambda _: 123)
    assert processes.observe(["python", "-m", "coire_node.training.worker"], 123, 10.0) == "unknown"
    assert child.poll.call_count == 1
