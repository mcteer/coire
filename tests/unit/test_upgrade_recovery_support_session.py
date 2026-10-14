"""Initial support session holds activation exclusion continuously into loop."""

import threading
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerClaim
from coire_core.settings import Settings


@pytest.mark.parametrize(
    "fault",
    [None, "cancel", "stop_failure", "cancel_after_stop", "activation_failure", "loop_failure"],
)
def test_initial_session_orders_retirement_activation_then_held_loop(tmp_path, monkeypatch, fault):
    from coire_node import expired_recovery_controller as controller
    from coire_node import expired_recovery_owner as owners
    from coire_node import expired_recovery_stop_reconciliation as retirement
    from coire_node import expired_recovery_support as worker

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    events = []
    stopped = threading.Event()
    if fault == "cancel":
        stopped.set()
    monkeypatch.setattr(
        worker,
        "wait_for_consumed_transfer",
        lambda *args: None if fault == "cancel" else owner.consumption,
    )

    def stop(*args, **kwargs):
        assert kwargs["stopped"] is stopped
        events.append("stop")
        if fault == "stop_failure":
            raise UpgradeUnavailable("fixture uncertain retirement")
        if fault == "cancel_after_stop":
            stopped.set()

    monkeypatch.setattr(retirement, "ensure_initial_retirement", stop)
    held = []

    @contextmanager
    def activate(*args):
        events.append("activate")
        if fault == "activation_failure":
            raise UpgradeUnavailable("fixture activation refused")
        held.append(True)
        try:
            yield owner
        finally:
            held.pop()
            events.append("release")

    monkeypatch.setattr(owners, "activated_recovery_owner", activate)

    def loop(*args):
        assert held
        assert args[1] == owner
        events.append("loop")
        if fault == "loop_failure":
            raise UpgradeUnavailable("fixture revoked loop")

    monkeypatch.setattr(controller, "run_held_controller", loop)

    def run():
        worker.run_initial_support_session(
            Settings(), owner.consumption.transfer.intent, owner.runtime, Mock(), stopped
        )

    if fault in {"stop_failure", "activation_failure", "loop_failure"}:
        with pytest.raises(UpgradeUnavailable):
            run()
    else:
        run()
    assert events == (
        []
        if fault == "cancel"
        else ["stop"]
        if fault in {"stop_failure", "cancel_after_stop"}
        else ["stop", "activate"]
        if fault == "activation_failure"
        else ["stop", "activate", "loop", "release"]
    )
    assert not held
