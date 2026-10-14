"""Successor publication preimages preserve original Source authority."""

from datetime import timedelta

import pytest
from pydantic import ValidationError
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoverySuccessionGrant,
    UpgradeExpiredRecoverySuccessionObservation,
    UpgradeExpiredRecoverySuccessorClaim,
)
from coire_core.models.upgrades import NativeGuardStatus, NativeUpgradeClaim


@pytest.mark.parametrize("fault", [None, "ready", "old_process", "source", "claim", "runtime"])
def test_successor_publication_exact_unready_preimages(tmp_path, fault):
    from coire_core.models.upgrade_recovery import UpgradeExpiredRecoverySuccessorPublicationIntent

    previous = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    now = previous.claimed_at + timedelta(seconds=1)
    proof = UpgradeExpiredRecoverySuccessionObservation(
        succession_id="01ARZ3NDEKTSV4RRFFQ69G5FAX",
        previous_owner=previous,
        successor=previous.process.model_copy(update={"pid": previous.process.pid + 1}),
        kernel_boot_identity=previous.kernel_boot_identity,
        preserved_source=previous.legacy_stop.preserved_source,
        observed_at=now,
        previous_owner_retired=True,
    )
    grant = UpgradeExpiredRecoverySuccessionGrant(
        observation=proof,
        user_id=previous.consumption.transfer.intent.accepted_by,
        key_id=None,
        credential_version=None,
        accepted_at=now,
        expires_at=now + timedelta(seconds=10),
    )
    owner = UpgradeExpiredRecoverySuccessorClaim(
        grant=grant,
        process=proof.successor,
        runtime=previous.runtime,
        kernel_boot_identity=previous.kernel_boot_identity,
        preserved_source=proof.preserved_source,
        claimed_at=now,
    )
    legacy = previous.consumption.transfer.legacy_guard
    plan = previous.consumption.transfer.intent.original_plan
    claim = NativeUpgradeClaim(
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        node=legacy.node,
        plan_sha256=plan.authority_sha256(),
        updated_at=previous.claimed_at,
    )
    status = NativeGuardStatus(
        node=legacy.node,
        bootstrap_runtime=owner.runtime,
        process=owner.process,
        selected=owner.preserved_source.selected,
        ready=False,
        observed_at=now,
    )
    data = {
        "owner": owner.model_dump(mode="json"),
        "original_claim": claim.model_dump(mode="json"),
        "before_runtime": legacy.bootstrap_runtime.model_dump(mode="json"),
        "before_status": legacy.model_dump(mode="json"),
        "published_status": status.model_dump(mode="json"),
    }
    if fault == "ready":
        data["published_status"]["ready"] = True
    elif fault == "old_process":
        data["published_status"]["process"] = previous.process.model_dump(mode="json")
    elif fault == "source":
        data["published_status"]["selected"]["generation"] += 1
    elif fault == "claim":
        data["original_claim"]["generation"] += 1
    elif fault == "runtime":
        data["before_runtime"]["runtime_sha256"] = "f" * 64
    if fault:
        with pytest.raises(ValidationError):
            UpgradeExpiredRecoverySuccessorPublicationIntent.model_validate(data)
    else:
        transition = UpgradeExpiredRecoverySuccessorPublicationIntent.model_validate(data)
        assert transition.original_claim == claim and transition.published_status.ready is False
