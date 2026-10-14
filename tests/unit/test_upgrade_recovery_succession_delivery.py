from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoverySuccessionDeliveryRead,
)


@pytest.mark.parametrize("fault", [None, "missing", "foreign", "revoked"])
async def test_delivery_pending_requires_exact_live_predecessor(tmp_path, monkeypatch, fault):
    from coire_api.upgrades import expired_recovery_succession_delivery as module

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    parent = (
        None
        if fault == "missing"
        else SimpleNamespace(owner_claim_payload=owner.model_dump(mode="json"))
    )
    session = SimpleNamespace(get=AsyncMock(side_effect=[parent, None]))
    monkeypatch.setattr(module, "operation_lock", AsyncMock())
    gate = AsyncMock(side_effect=UpgradeUnavailable("revoked") if fault == "revoked" else None)
    monkeypatch.setattr(module, "verify_live_owner", gate)
    request = UpgradeExpiredRecoverySuccessionDeliveryRead(
        succession_id="01ARZ3NDEKTSV4RRFFQ69G5FAX",
        predecessor_sha256="f" * 64 if fault == "foreign" else owner.sha256(),
        successor=owner.process.model_copy(update={"pid": owner.process.pid + 1}),
    )
    if fault:
        with pytest.raises(UpgradeForbidden if fault == "foreign" else UpgradeUnavailable):
            await module.read_succession_delivery(
                session,
                owner.consumption.transfer.intent.recovery_id,
                request,
                owner.consumption.transfer.legacy_guard.node,
            )
    else:
        result = await module.read_succession_delivery(
            session,
            owner.consumption.transfer.intent.recovery_id,
            request,
            owner.consumption.transfer.legacy_guard.node,
        )
        assert result.grant is None and result.request == request
        gate.assert_awaited_once()
