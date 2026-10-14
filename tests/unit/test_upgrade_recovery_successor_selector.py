"""Configured successor selector never silently becomes legacy mode."""

import pytest
from pydantic import ValidationError

from coire_core.settings import Settings


@pytest.mark.parametrize("fault", [None, "no_parent", "same", "invalid"])
def test_successor_selector_requires_distinct_scoped_parent(fault):
    data = {
        "upgrade_recovery_id": "01ARZ3NDEKTSV4RRFFQ69G5FAW",
        "upgrade_recovery_succession_id": "01ARZ3NDEKTSV4RRFFQ69G5FAX",
    }
    if fault == "no_parent":
        data["upgrade_recovery_id"] = None
    elif fault == "same":
        data["upgrade_recovery_succession_id"] = data["upgrade_recovery_id"]
    elif fault == "invalid":
        data["upgrade_recovery_succession_id"] = "caller/path"
    if fault:
        with pytest.raises(ValidationError):
            Settings(**data)
    else:
        settings = Settings(**data)
        assert settings.upgrade_recovery_succession_id == data["upgrade_recovery_succession_id"]


@pytest.mark.parametrize("mode", ["resume", "initial", "caller-mode"])
def test_succession_mode_is_explicit_and_scoped(mode):
    if mode == "caller-mode":
        with pytest.raises(ValidationError):
            Settings(upgrade_recovery_succession_mode=mode)
        return
    if mode == "initial":
        with pytest.raises(ValidationError):
            Settings(upgrade_recovery_succession_mode=mode)
    settings = Settings(
        upgrade_recovery_id="01ARZ3NDEKTSV4RRFFQ69G5FAW",
        upgrade_recovery_succession_id="01ARZ3NDEKTSV4RRFFQ69G5FAX",
        upgrade_recovery_succession_mode=mode,
    )
    assert settings.upgrade_recovery_succession_mode == mode


@pytest.mark.parametrize("accepted", [False, True])
def test_cancel_during_successor_state_probe_never_enters_owner_context(monkeypatch, accepted):
    import threading
    from contextlib import contextmanager
    from types import SimpleNamespace
    from unittest.mock import Mock

    from coire_node import expired_recovery_successor_controller as worker

    claim = Mock()
    stopped = threading.Event()
    entered = Mock()

    @contextmanager
    def owner_context(*args):
        entered()
        yield claim

    def probe(*args):
        stopped.set()
        return SimpleNamespace(owner=claim if accepted else None)

    monkeypatch.setattr(worker, "initial_successor_owner", owner_context)
    monkeypatch.setattr(worker, "resumed_successor_owner", owner_context)
    held = Mock()
    monkeypatch.setattr(worker, "run_held_successor", held)
    control = SimpleNamespace(probe_recovery_successor=Mock(side_effect=probe))
    worker.run_successor_holding(Settings(), claim, control, stopped)
    entered.assert_not_called()
    held.assert_not_called()
    control.probe_recovery_successor.assert_called_once_with(claim)
