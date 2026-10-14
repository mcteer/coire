"""No database snapshot transaction stays held across trusted observer I/O."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_api.auth import Principal, PrincipalKind
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.auth import UserRole
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoverySuccessionObservation,
)


@pytest.mark.parametrize("foreign", [False, True, "actor", "timeout", "cancelled"])
async def test_succession_observer_runs_outside_snapshot(tmp_path, monkeypatch, foreign):
    from coire_api.upgrades import expired_recovery_succession_orchestration as module

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    active = [False]
    row = SimpleNamespace(
        user_id=owner.consumption.transfer.intent.accepted_by,
        key_id=None,
        credential_version=None,
        owner_claim_payload=owner.model_dump(mode="json"),
    )
    session = SimpleNamespace(get=AsyncMock(side_effect=[None, row]))

    @asynccontextmanager
    async def transaction():
        assert not active[0]
        active[0] = True
        try:
            yield session
        finally:
            active[0] = False

    sessions = SimpleNamespace(begin=transaction)
    monkeypatch.setattr(module, "operation_lock", AsyncMock())
    monkeypatch.setattr(module, "authorize", AsyncMock())
    monkeypatch.setattr(module, "verify_live_owner", AsyncMock(return_value=owner))
    accept = AsyncMock(return_value=(object(), True))
    monkeypatch.setattr(module, "accept_succession", accept)
    identifier = "01ARZ3NDEKTSV4RRFFQ69G5FAX"

    async def observe(previous, identity):
        assert not active[0] and previous == owner and identity == identifier
        if foreign == "timeout":
            raise TimeoutError()
        if foreign == "cancelled":
            raise asyncio.CancelledError()
        return UpgradeExpiredRecoverySuccessionObservation(
            succession_id="01ARZ3NDEKTSV4RRFFQ69G5FAY" if foreign else identifier,
            previous_owner=owner,
            successor=owner.process.model_copy(update={"pid": owner.process.pid + 1}),
            kernel_boot_identity=owner.kernel_boot_identity,
            preserved_source=owner.legacy_stop.preserved_source,
            observed_at=owner.claimed_at + timedelta(seconds=1),
            previous_owner_retired=True,
        )

    actor = Principal(
        kind=PrincipalKind.ADMIN,
        user_id=uuid4() if foreign == "actor" else row.user_id,
        role=UserRole.ADMIN,
    )
    if foreign:
        expected_error = (
            asyncio.CancelledError
            if foreign == "cancelled"
            else UpgradeUnavailable
            if foreign == "timeout"
            else UpgradeForbidden
            if foreign == "actor"
            else UpgradeConflict
        )
        with pytest.raises(expected_error):
            await module.accept_with_successor_observation(
                sessions, actor, owner, identifier, observe
            )
        accept.assert_not_awaited()
    else:
        await module.accept_with_successor_observation(sessions, actor, owner, identifier, observe)
        accept.assert_awaited_once()
    assert not active[0]
