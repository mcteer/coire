"""Fresh fixed-node passive delivery; absence/errors never grant an effect."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import UpgradeExpiredRecoveryRow
from coire_api.upgrades.expired_recovery_consumption import verify_live_consumption
from coire_api.upgrades.expired_recovery_owner import read_live_support_context
from coire_core.errors import UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryConsumptionState,
    UpgradeExpiredRecoveryTransferConsumption,
)
from coire_core.models.upgrades import NodeAgentProcessIdentity


async def read_consumption_state(
    session: AsyncSession,
    recovery_id: str,
    intent_sha256: str,
    process: NodeAgentProcessIdentity,
    node: str,
) -> UpgradeExpiredRecoveryConsumptionState:
    """No mutation, recaptured budget, caller target or error-to-pending fallback.

    Positive pending is allowed only under live accepted/transferring authority.
    A consumed reply requires the exact initial consumer/window; accepted owner
    state requires separate explicit resume, never passive initial activation.
    """
    intent = await read_live_support_context(session, recovery_id, node)
    if (
        intent.sha256() != intent_sha256
        or process.environment_sha256 != intent.support_release.manifest.environment_sha256
    ):
        raise UpgradeForbidden()
    row = await session.get(UpgradeExpiredRecoveryRow, recovery_id, with_for_update=True)
    if row is None or row.owner_claim_payload is not None:
        raise UpgradeUnavailable("Recovery delivery requires initial unaccepted authority")
    if row.consumption_payload is None:
        if row.phase not in {"accepted", "transferring"}:
            raise UpgradeUnavailable("Recovery pending delivery state is inconsistent")
        receipt = None
    else:
        retained = UpgradeExpiredRecoveryTransferConsumption.model_validate(row.consumption_payload)
        receipt = await verify_live_consumption(
            session, recovery_id, retained.sha256(), process, node
        )
    checked_at = datetime.now(UTC)
    if not intent.accepted_at <= checked_at < intent.deadline_at:
        raise UpgradeUnavailable("Recovery delivery exhausted captured budget")
    return UpgradeExpiredRecoveryConsumptionState(
        intent_sha256=intent.sha256(), consumption=receipt, checked_at=checked_at
    )
