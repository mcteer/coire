"""Transfer reservation remains intent only; repeat cannot renew or change it."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_upgrade_expired_recovery_transfer import transfer_payload

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerTransfer
from coire_core.models.upgrades import UpgradePhase


async def test_transfer_prepare_retains_exact_intent_and_audit_without_changing_original(
    tmp_path, monkeypatch
):
    from coire_api.upgrades import expired_recovery_transfer as service

    transfer = UpgradeExpiredRecoveryOwnerTransfer.model_validate(transfer_payload(tmp_path))
    intent = transfer.intent
    plan = intent.original_plan
    original = SimpleNamespace(
        active_slot=1,
        phase="recovery_required",
        payload=plan.operation.model_copy(
            update={"phase": UpgradePhase.RECOVERY_REQUIRED}
        ).model_dump(mode="json"),
        execution_plan=plan.model_dump(mode="json"),
    )
    row = SimpleNamespace(
        user_id=intent.accepted_by,
        key_id=None,
        credential_version=None,
        intent_sha256=intent.sha256(),
        intent_payload=intent.model_dump(mode="json"),
        active_slot=1,
        phase="accepted",
        transfer_payload=None,
    )
    fences = [
        SimpleNamespace(target=target, released=False, generation=plan.operation.generation)
        for target in plan.maintenance.targets
    ]
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[row, original]),
        flush=AsyncMock(),
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: fences)),
    )
    principal = SimpleNamespace(api_key_id=None, credential_version=None)
    monkeypatch.setattr(service, "operation_lock", AsyncMock())
    monkeypatch.setattr(service, "authorize", AsyncMock(return_value=intent.accepted_by))
    monkeypatch.setattr(service, "get_release", AsyncMock(return_value=intent.support_release))
    audit = AsyncMock()
    monkeypatch.setattr(service, "write_principal_audit", audit)
    retained, created = await service.prepare_transfer(session, principal, transfer)
    assert retained == transfer and created and row.phase == "transferring"
    assert original.active_slot == 1 and original.phase == "recovery_required"
    audit.assert_awaited_once()
    session.get = AsyncMock(return_value=row)
    retained, created = await service.prepare_transfer(session, principal, transfer)
    assert retained == transfer and not created
    conflicting = transfer.model_copy(update={"transfer_id": uuid4()})
    with pytest.raises(UpgradeConflict):
        await service.prepare_transfer(session, principal, conflicting)
    assert audit.await_count == 1
