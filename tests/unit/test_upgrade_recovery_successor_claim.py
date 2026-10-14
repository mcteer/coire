"""Successor claim DTO binds exact grant, not a fabricated initial consumer."""

from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoverySuccessionGrant,
    UpgradeExpiredRecoverySuccessionObservation,
)


@pytest.mark.parametrize(
    "fault", [None, "process", "kernel", "runtime", "source", "early", "expired"]
)
def test_successor_claim_exact_scope_and_initial_window(tmp_path, fault):
    from coire_core.models.upgrade_recovery import UpgradeExpiredRecoverySuccessorClaim

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    now = owner.claimed_at + timedelta(seconds=1)
    successor = owner.process.model_copy(update={"pid": owner.process.pid + 1})
    proof = UpgradeExpiredRecoverySuccessionObservation(
        succession_id="01ARZ3NDEKTSV4RRFFQ69G5FAX",
        previous_owner=owner,
        successor=successor,
        kernel_boot_identity=owner.kernel_boot_identity,
        preserved_source=owner.legacy_stop.preserved_source,
        observed_at=now,
        previous_owner_retired=True,
    )
    grant = UpgradeExpiredRecoverySuccessionGrant(
        observation=proof,
        user_id=uuid4(),
        key_id=None,
        credential_version=None,
        accepted_at=now,
        expires_at=now + timedelta(seconds=10),
    )
    data = {
        "grant": grant.model_dump(mode="json"),
        "process": successor.model_dump(mode="json"),
        "runtime": owner.runtime.model_dump(mode="json"),
        "kernel_boot_identity": owner.kernel_boot_identity,
        "preserved_source": owner.legacy_stop.preserved_source.model_dump(mode="json"),
        "claimed_at": now.isoformat(),
    }
    if fault == "process":
        data["process"] = owner.process.model_dump(mode="json")
    elif fault == "kernel":
        data["kernel_boot_identity"] = "foreign-boot"
    elif fault == "runtime":
        data["runtime"]["runtime_sha256"] = "f" * 64
    elif fault == "source":
        data["preserved_source"]["process"]["pid"] += 1
    elif fault == "early":
        data["claimed_at"] = (now - timedelta(seconds=1)).isoformat()
    elif fault == "expired":
        data["claimed_at"] = grant.expires_at.isoformat()
    if fault:
        with pytest.raises(ValidationError):
            UpgradeExpiredRecoverySuccessorClaim.model_validate(data)
    else:
        claim = UpgradeExpiredRecoverySuccessorClaim.model_validate(data)
        assert claim.process != owner.consumption.consumer
        assert claim.grant.observation.previous_owner == owner
        assert claim.grant.observation.previous_owner.consumption == owner.consumption
