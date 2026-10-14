"""Canonical ledger checks are separate from real native ownership."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoverySuccessionGrant,
    UpgradeExpiredRecoverySuccessionObservation,
)


@pytest.mark.parametrize("fault", [None, "hash", "process", "expiry", "missing"])
async def test_live_succession_gate_exact_ledger_and_window(tmp_path, monkeypatch, fault):
    from coire_api.upgrades import expired_recovery_succession as module

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    now = owner.claimed_at + timedelta(seconds=1)
    proof = UpgradeExpiredRecoverySuccessionObservation(
        succession_id="01ARZ3NDEKTSV4RRFFQ69G5FAX",
        previous_owner=owner,
        successor=owner.process.model_copy(update={"pid": owner.process.pid + 1}),
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
    entry = SimpleNamespace(
        recovery_id=owner.consumption.transfer.intent.recovery_id,
        predecessor_sha256=owner.sha256(),
        grant_sha256=grant.sha256(),
        grant_payload=grant.model_dump(mode="json"),
    )
    recovery = SimpleNamespace(
        user_id=grant.user_id,
        key_id=None,
        credential_version=None,
        owner_claim_payload=owner.model_dump(mode="json"),
        active_slot=1,
        phase="recovering",
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[None if fault == "missing" else entry, recovery])
    )
    monkeypatch.setattr(module, "operation_lock", AsyncMock())
    monkeypatch.setattr(module, "verify_binding", AsyncMock())
    monkeypatch.setattr(
        module,
        "datetime",
        SimpleNamespace(now=lambda zone: grant.expires_at if fault == "expiry" else now),
    )
    digest = "f" * 64 if fault == "hash" else grant.sha256()
    process = owner.process if fault == "process" else proof.successor
    if fault:
        with pytest.raises((UpgradeForbidden, UpgradeUnavailable)):
            await module.verify_live_succession_grant(
                session, proof.succession_id, digest, process, "studio-a"
            )
    else:
        assert (
            await module.verify_live_succession_grant(
                session, proof.succession_id, digest, process, "studio-a"
            )
            == grant
        )
        module.verify_binding.assert_awaited_once()
