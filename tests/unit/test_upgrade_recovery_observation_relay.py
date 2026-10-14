from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoverySuccessionObservation,
)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "absent",
        "stale",
        "fork",
        "replay",
        "future",
        "capacity",
        "regressed",
        "lock_delay",
        "read_delay",
        "replay_after_prune",
        "wall_rollback_after_prune",
    ],
)
def test_ephemeral_relay_is_not_authority(tmp_path, monkeypatch, fault):
    from coire_api.upgrades import expired_recovery_observation_relay as module

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    now = owner.claimed_at + timedelta(seconds=1)
    clock = [now, 0.0]
    monkeypatch.setattr(module, "datetime", SimpleNamespace(now=lambda zone: clock[0]))
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[1]))
    proof = UpgradeExpiredRecoverySuccessionObservation(
        succession_id="01ARZ3NDEKTSV4RRFFQ69G5FAX",
        previous_owner=owner,
        successor=owner.process.model_copy(update={"pid": owner.process.pid + 1}),
        kernel_boot_identity=owner.kernel_boot_identity,
        preserved_source=owner.legacy_stop.preserved_source,
        observed_at=now,
        previous_owner_retired=True,
    )
    relay = module.SuccessionObservationRelay()
    if fault == "lock_delay":

        @contextmanager
        def delayed_lock():
            clock[0] = now + timedelta(seconds=6)
            clock[1] = 6.0
            yield

        monkeypatch.setattr(relay, "_lock", delayed_lock())
        with pytest.raises(UpgradeUnavailable):
            relay.publish(proof)
        assert relay._entries == {}
        return
    if fault == "future":
        with pytest.raises(UpgradeUnavailable):
            relay.publish(proof.model_copy(update={"observed_at": now + timedelta(seconds=1)}))
        return
    if fault != "absent":
        relay.publish(proof)
    if fault == "wall_rollback_after_prune":
        clock[0] = now + timedelta(seconds=6)
        clock[1] = 1.0
        with pytest.raises(UpgradeUnavailable):
            relay.read(owner, proof.succession_id)
        clock[0] = now
        with pytest.raises(UpgradeUnavailable):
            relay.publish(proof)
        assert relay._entries == {}
        return
    if fault == "replay_after_prune":
        clock[1] = 6.0
        with pytest.raises(UpgradeUnavailable):
            relay.read(owner, proof.succession_id)
        with pytest.raises(UpgradeUnavailable):
            relay.publish(proof)
        assert relay._entries == {}
        return
    if fault == "read_delay":
        validate = UpgradeExpiredRecoverySuccessionObservation.model_validate_json

        def delayed_validate(value):
            result = validate(value)
            clock[0] = now + timedelta(seconds=6)
            clock[1] = 6.0
            return result

        monkeypatch.setattr(
            UpgradeExpiredRecoverySuccessionObservation, "model_validate_json", delayed_validate
        )
        with pytest.raises(UpgradeUnavailable):
            relay.read(owner, proof.succession_id)
        assert relay._entries == {}
        return
    if fault == "capacity":
        alphabet = "0123456789ABCDEFG"
        for char in alphabet[:15]:
            relay.publish(
                proof.model_copy(update={"succession_id": "01ARZ3NDEKTSV4RRFFQ69G5FA" + char})
            )
        with pytest.raises(UpgradeUnavailable):
            relay.publish(
                proof.model_copy(
                    update={"succession_id": "01ARZ3NDEKTSV4RRFFQ69G5FA" + alphabet[15]}
                )
            )
    if fault == "regressed":
        with pytest.raises(UpgradeConflict):
            relay.publish(proof.model_copy(update={"observed_at": now - timedelta(milliseconds=1)}))
    if fault == "fork":
        with pytest.raises(UpgradeConflict):
            relay.publish(
                proof.model_copy(
                    update={
                        "successor": proof.successor.model_copy(
                            update={"pid": proof.successor.pid + 1}
                        )
                    }
                )
            )
    if fault == "replay":
        clock[1] = 4.0
        relay.publish(proof)
        clock[1] = 6.0
    if fault == "stale":
        clock[0] = now + timedelta(seconds=6)
    if fault in {"absent", "stale", "replay"}:
        with pytest.raises(UpgradeUnavailable):
            relay.read(owner, proof.succession_id)
    else:
        returned = relay.read(owner, proof.succession_id)
        assert returned == proof
        with pytest.raises(ValidationError, match="frozen"):
            returned.successor.pid += 1
        assert returned is not relay.read(owner, proof.succession_id)
        assert relay.read(owner, proof.succession_id) == proof
    with pytest.raises(UpgradeUnavailable):
        module.SuccessionObservationRelay().read(owner, proof.succession_id)
