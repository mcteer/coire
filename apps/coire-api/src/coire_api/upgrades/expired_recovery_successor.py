"""Distinct immutable successor acceptance; physical lifetime ownership is external."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import (
    UpgradeExpiredRecoveryRow,
    UpgradeRecoverySuccessionRow,
    UpgradeRecoverySuccessorClaimRow,
)
from coire_api.upgrades.expired_recovery_owner import verify_binding
from coire_api.upgrades.expired_recovery_succession import verify_live_succession_grant
from coire_api.upgrades.persistence import operation_lock
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoverySuccessorClaim,
    UpgradeExpiredRecoverySuccessorState,
)
from coire_core.models.upgrades import NodeAgentProcessIdentity


async def retained_binding(
    session: AsyncSession, claim: UpgradeExpiredRecoverySuccessorClaim, node: str
) -> tuple[UpgradeExpiredRecoveryRow, Principal]:
    grant = claim.grant
    proof = grant.observation
    owner = proof.previous_owner
    intent = owner.consumption.transfer.intent
    entry = await session.get(
        UpgradeRecoverySuccessionRow, proof.succession_id, with_for_update=True
    )
    if (
        entry is None
        or entry.grant_sha256 != grant.sha256()
        or entry.grant_payload != grant.model_dump(mode="json")
        or entry.recovery_id != intent.recovery_id
        or entry.predecessor_sha256 != owner.sha256()
    ):
        raise UpgradeConflict("Successor retained grant differs")
    row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id, with_for_update=True)
    if row is None or row.owner_claim_payload != owner.model_dump(mode="json"):
        raise UpgradeConflict("Successor original owner differs")
    actor = await verify_binding(session, row, owner, node)
    if (row.user_id, row.key_id, row.credential_version) != (
        grant.user_id,
        grant.key_id,
        grant.credential_version,
    ):
        raise UpgradeForbidden()
    return row, actor


async def accept_successor_claim(
    session: AsyncSession, claim: UpgradeExpiredRecoverySuccessorClaim, node: str
) -> tuple[UpgradeExpiredRecoverySuccessorClaim, bool]:
    """Fixed authenticated node producer must hold actual private exclusion.

    Core metadata/audit alone does not prove ownership. Exact replay retains the
    initial epoch, including after transfer expiry; it permits no native effect.
    """
    claim = UpgradeExpiredRecoverySuccessorClaim.model_validate_json(claim.model_dump_json())
    await operation_lock(session)
    row, actor = await retained_binding(session, claim, node)
    succession_id = claim.grant.observation.succession_id
    existing = await session.get(
        UpgradeRecoverySuccessorClaimRow, succession_id, with_for_update=True
    )
    if existing is not None:
        previous = UpgradeExpiredRecoverySuccessorClaim.model_validate(existing.claim_payload)
        if previous.sha256() != claim.sha256() or existing.claim_sha256 != previous.sha256():
            raise UpgradeConflict("Successor claim already differs")
        return previous, False
    await verify_live_succession_grant(
        session, succession_id, claim.grant.sha256(), claim.process, node
    )
    if not 0 <= (datetime.now(UTC) - claim.claimed_at).total_seconds() <= 5:
        raise UpgradeUnavailable("Successor claim is stale")
    session.add(
        UpgradeRecoverySuccessorClaimRow(
            id=succession_id,
            claim_sha256=claim.sha256(),
            claim_payload=claim.model_dump(mode="json"),
        )
    )
    await write_principal_audit(
        session,
        principal=actor,
        action="upgrades.expired_recovery.successor.accept",
        target_type="upgrade_recovery",
        target_id=row.id,
        detail={
            "succession_id": succession_id,
            "claim_sha256": claim.sha256(),
            "original_job_id": row.original_job_id,
        },
    )
    await session.flush()
    return claim, True


async def verify_live_successor(
    session: AsyncSession,
    succession_id: str,
    claim_sha256: str,
    process: NodeAgentProcessIdentity,
    node: str,
) -> UpgradeExpiredRecoverySuccessorClaim:
    """Accepted exact identity resume uses original root budget, never renews it.

    Actual current PID/birth/runtime/kernel/Source and held descriptor capability
    are mandatory at the caller; retained DTO alone is never effect authority.
    """
    await operation_lock(session)
    entry = await session.get(UpgradeRecoverySuccessorClaimRow, succession_id, with_for_update=True)
    if entry is None:
        raise UpgradeUnavailable("Successor has no retained claim")
    claim = UpgradeExpiredRecoverySuccessorClaim.model_validate(entry.claim_payload)
    if (
        entry.claim_sha256 != claim.sha256()
        or claim.sha256() != claim_sha256
        or claim.process != process
        or claim.grant.observation.succession_id != succession_id
    ):
        raise UpgradeForbidden()
    row, _ = await retained_binding(session, claim, node)
    intent = claim.grant.observation.previous_owner.consumption.transfer.intent
    if (
        row.active_slot != 1
        or row.phase not in {"recovering", "verifying"}
        or not claim.claimed_at <= datetime.now(UTC) < intent.deadline_at
    ):
        raise UpgradeUnavailable("Successor original recovery budget is no longer live")
    return claim


async def probe_successor_acceptance(
    session: AsyncSession,
    succession_id: str,
    claim_sha256: str,
    process: NodeAgentProcessIdentity,
    node: str,
    *,
    grant_sha256: str,
) -> UpgradeExpiredRecoverySuccessorState:
    """Resolve lost commit response without interpreting errors as unaccepted.

    Positive unaccepted requires exact live initial grant/process; accepted
    identity uses the original recovery budget. Fixed authenticated transport and
    actual local lifetime/physical verification remain separate requirements.
    """
    await operation_lock(session)
    entry = await session.get(UpgradeRecoverySuccessorClaimRow, succession_id, with_for_update=True)
    if entry is None:
        grant_entry = await session.get(
            UpgradeRecoverySuccessionRow, succession_id, with_for_update=True
        )
        if grant_entry is None:
            raise UpgradeUnavailable("Successor state has no accepted grant")
        if grant_entry.grant_sha256 != grant_sha256:
            raise UpgradeForbidden()
        await verify_live_succession_grant(
            session, succession_id, grant_entry.grant_sha256, process, node
        )
        owner = None
    else:
        owner = await verify_live_successor(session, succession_id, claim_sha256, process, node)
        if owner.grant.sha256() != grant_sha256:
            raise UpgradeForbidden()
    return UpgradeExpiredRecoverySuccessorState(
        claim_sha256=claim_sha256, owner=owner, checked_at=datetime.now(UTC)
    )
