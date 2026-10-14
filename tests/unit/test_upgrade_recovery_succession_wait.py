import threading
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerClaim
from coire_core.settings import Settings


@pytest.mark.parametrize(
    "fault",
    [
        "cancelled",
        "pending",
        "error",
        "expired",
        "granted",
        "late_identity",
        "late_expiry",
        "grant_expired",
        "monotonic_expiry",
        "publish",
        "publish_error",
        "observation_changed",
        "observation_expired",
        "identity_cancel",
    ],
)
def test_passive_succession_wait_never_falls_back(tmp_path, monkeypatch, fault):
    from coire_node import expired_recovery_succession_wait as worker

    previous = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    intent = previous.consumption.transfer.intent
    now = intent.deadline_at if fault == "expired" else previous.claimed_at + timedelta(seconds=1)
    monkeypatch.setattr(worker, "datetime", SimpleNamespace(now=lambda zone: now))
    tick = [0.0]
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: tick[0]), raising=False)
    identity = Mock(
        return_value=previous.process.model_copy(update={"pid": previous.process.pid + 1})
    )
    monkeypatch.setattr(worker, "verify_passive_support_identity", identity)
    successor = identity.return_value
    if fault == "late_identity":
        identity.side_effect = [successor, successor.model_copy(update={"pid": successor.pid + 1})]
    grant = SimpleNamespace(expires_at=now + timedelta(seconds=1))
    cancel = threading.Event()
    if fault == "cancelled":
        cancel.set()
    if fault == "identity_cancel":

        def cancel_identity(*args):
            cancel.set()
            return successor

        identity.side_effect = cancel_identity

    from coire_node import expired_recovery_succession as observer

    sample = SimpleNamespace(
        successor=successor.model_copy(update={"pid": successor.pid + 1})
        if fault == "observation_changed"
        else successor
    )

    def observe(*args):
        nonlocal now
        if fault == "observation_expired":
            tick[0] = 1000.0
            now -= timedelta(seconds=60)
        return sample

    observation = Mock(side_effect=observe)
    monkeypatch.setattr(observer, "observe_same_boot_succession", observation)
    publication = Mock(
        side_effect=UpgradeUnavailable("fixture lost publication acknowledgement")
        if fault == "publish_error"
        else lambda *_: cancel.set()
    )

    def poll(*args):
        nonlocal now
        if fault in {"publish", "publish_error", "observation_changed", "observation_expired"}:
            return SimpleNamespace(grant=None)
        if fault == "late_expiry":
            now = intent.deadline_at
            return SimpleNamespace(grant=None)
        if fault == "monotonic_expiry":
            tick[0] = 1000.0
            now -= timedelta(seconds=60)
            return SimpleNamespace(grant=grant)
        if fault == "grant_expired":
            now = grant.expires_at
        if fault in {"granted", "late_identity", "grant_expired"}:
            return SimpleNamespace(grant=grant)
        if fault == "error":
            raise UpgradeUnavailable("fixture unavailable")
        cancel.set()
        return SimpleNamespace(grant=None)

    control = SimpleNamespace(
        read_recovery_succession=Mock(side_effect=poll),
        publish_recovery_succession_observation=publication,
    )
    if fault in {
        "error",
        "expired",
        "late_identity",
        "late_expiry",
        "grant_expired",
        "monotonic_expiry",
        "publish_error",
        "observation_changed",
        "observation_expired",
    }:
        with pytest.raises(UpgradeUnavailable):
            worker.wait_for_succession_grant(
                Settings(), previous, "01ARZ3NDEKTSV4RRFFQ69G5FAX", control, cancel
            )
    elif fault == "granted":
        assert (
            worker.wait_for_succession_grant(
                Settings(), previous, "01ARZ3NDEKTSV4RRFFQ69G5FAX", control, cancel
            )
            is grant
        )
    else:
        assert (
            worker.wait_for_succession_grant(
                Settings(), previous, "01ARZ3NDEKTSV4RRFFQ69G5FAX", control, cancel
            )
            is None
        )
    assert control.read_recovery_succession.call_count == (
        0 if fault in {"cancelled", "expired", "identity_cancel"} else 1
    )
    assert publication.call_count == (1 if fault in {"publish", "publish_error"} else 0)
    assert observation.call_count == (
        1
        if fault in {"publish", "publish_error", "observation_changed", "observation_expired"}
        else 0
    )
    assert identity.call_count == control.read_recovery_succession.call_count + (
        1 if fault in {"granted", "late_identity", "identity_cancel"} else 0
    )
