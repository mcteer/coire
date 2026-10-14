"""Fresh explicit grant delivery; missing ledger alone is never pending authority."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import UpgradeExpiredRecoveryRow, UpgradeRecoverySuccessionRow
from coire_api.upgrades.expired_recovery_owner import verify_live_owner
from coire_api.upgrades.expired_recovery_succession import verify_live_succession_grant
from coire_api.upgrades.persistence import operation_lock
from coire_core.errors import UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoverySuccessionDeliveryRead,
    UpgradeExpiredRecoverySuccessionDeliveryState,
)


async def read_succession_delivery(
    session: AsyncSession,
    recovery_id: str,
    request: UpgradeExpiredRecoverySuccessionDeliveryRead,
    node: str,
) -> UpgradeExpiredRecoverySuccessionDeliveryState:
    """Fixed authenticated node only; grant DTO is not physical owner permission."""
    await operation_lock(session)
    parent = await session.get(UpgradeExpiredRecoveryRow, recovery_id, with_for_update=True)
    if parent is None or parent.owner_claim_payload is None:
        raise UpgradeUnavailable("Succession delivery predecessor absent")
    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(parent.owner_claim_payload)
    if (
        owner.sha256() != request.predecessor_sha256
        or request.successor == owner.process
        or request.successor.environment_sha256 != owner.runtime.environment_sha256
    ):
        raise UpgradeForbidden()
    entry = await session.get(
        UpgradeRecoverySuccessionRow, request.succession_id, with_for_update=True
    )
    if entry is None:
        await verify_live_owner(session, recovery_id, owner.sha256(), owner.process, node)
        grant = None
    else:
        grant = await verify_live_succession_grant(
            session, request.succession_id, entry.grant_sha256, request.successor, node
        )
        if grant.observation.previous_owner != owner:
            raise UpgradeForbidden()
    checked_at = datetime.now(UTC)
    if grant is not None and not grant.accepted_at <= checked_at < grant.expires_at:
        raise UpgradeUnavailable("Succession delivery initial handover expired")
    return UpgradeExpiredRecoverySuccessionDeliveryState(
        request=request, grant=grant, checked_at=checked_at
    )
