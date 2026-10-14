"""Bounded status replacement retries precede all fixture restart effects."""

from unittest.mock import Mock

import pytest

from coire_core.errors import UpgradeUnavailable
from coire_node import upgrade_native_terminal_supervisor as supervisor


def test_atomic_status_replacement_retries_without_restarting_children(monkeypatch):
    children = (object(), object())
    read = Mock(side_effect=[UpgradeUnavailable("retired opened inode"), children])
    monkeypatch.setattr(supervisor, "current_children", read)
    launch = Mock()
    monkeypatch.setattr(supervisor.subprocess, "Popen", launch)
    assert supervisor.stable_current_children(Mock()) == children
    assert read.call_count == 2
    launch.assert_not_called()


def test_persistent_unknown_status_refuses_within_original_read_budget(monkeypatch):
    clock = iter([0, 0.5, 1, 1.5, 2])
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(supervisor.time, "sleep", lambda _: None)
    read = Mock(side_effect=UpgradeUnavailable("unknown ownership"))
    monkeypatch.setattr(supervisor, "current_children", read)
    launch = Mock()
    monkeypatch.setattr(supervisor.subprocess, "Popen", launch)
    with pytest.raises(UpgradeUnavailable, match="unknown ownership"):
        supervisor.stable_current_children(Mock())
    assert read.call_count == 4
    launch.assert_not_called()
