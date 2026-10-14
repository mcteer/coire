"""Append-only succession scope acceptance; no native takeover by DTO alone."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import UpgradeExpiredRecoveryRow, UpgradeRecoverySuccessionRow
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.expired_recovery_owner import verify_binding, verify_live_owner
from coire_api.upgrades.persistence import operation_lock
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoverySuccessionGrant,
    UpgradeExpiredRecoverySuccessionObservation,
)
from coire_core.models.upgrades import NodeAgentProcessIdentity


async def accept_succession(
    session: AsyncSession,
    principal: Principal,
    observation: UpgradeExpiredRecoverySuccessionObservation,
    node: str,
) -> tuple[UpgradeExpiredRecoverySuccessionGrant, bool]:
    """Internal orchestration only: observation must come from trusted node producer.

    Never expose a caller-submitted observation as physical authority. Same
    initiating admin/credential is conservatively required; any new-actor flow
    needs its own specified authorization. Replay inspects exact immutable scope,
    not renews the window. Caller transaction owns audit+grant atomic commit.
    """
    observation = UpgradeExpiredRecoverySuccessionObservation.model_validate_json(
        observation.model_dump_json()
    )
    owner = observation.previous_owner
    intent = owner.consumption.transfer.intent
    await operation_lock(session)
    await authorize(session, principal)
    if principal.user_id is None:
        raise UpgradeForbidden()
    row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id, with_for_update=True)
    if row is None:
        raise UpgradeUnavailable("Succession original recovery is absent")
    if (principal.user_id, principal.api_key_id, principal.credential_version) != (
        row.user_id,
        row.key_id,
        row.credential_version,
    ):
        raise UpgradeForbidden()
    await verify_binding(session, row, owner, node)
    if row.owner_claim_payload != owner.model_dump(mode="json"):
        raise UpgradeConflict("Succession predecessor owner differs")
    previous = await session.scalar(
        select(UpgradeRecoverySuccessionRow).where(
            UpgradeRecoverySuccessionRow.recovery_id == intent.recovery_id,
            UpgradeRecoverySuccessionRow.predecessor_sha256 == owner.sha256(),
        )
    )
    if previous is not None:
        grant = UpgradeExpiredRecoverySuccessionGrant.model_validate(previous.grant_payload)
        if (
            previous.id != observation.succession_id
            or grant.observation != observation
            or previous.grant_sha256 != grant.sha256()
        ):
            raise UpgradeConflict("Succession predecessor already has a different grant")
        return grant, False
    if await session.get(UpgradeRecoverySuccessionRow, observation.succession_id) is not None:
        raise UpgradeConflict("Succession identity already belongs to another scope")
    await verify_live_owner(session, intent.recovery_id, owner.sha256(), owner.process, node)
    now = datetime.now(UTC)
    if not 0 <= (now - observation.observed_at).total_seconds() <= 5:
        raise UpgradeUnavailable("Succession physical observation is stale")
    grant = UpgradeExpiredRecoverySuccessionGrant(
        observation=observation,
        user_id=principal.user_id,
        key_id=principal.api_key_id,
        credential_version=principal.credential_version,
        accepted_at=now,
        expires_at=min(now + timedelta(seconds=30), intent.deadline_at),
    )
    session.add(
        UpgradeRecoverySuccessionRow(
            id=observation.succession_id,
            recovery_id=intent.recovery_id,
            predecessor_sha256=owner.sha256(),
            grant_sha256=grant.sha256(),
            grant_payload=grant.model_dump(mode="json"),
        )
    )
    await write_principal_audit(
        session,
        principal=principal,
        action="upgrades.expired_recovery.succession.accept",
        target_type="upgrade_recovery",
        target_id=intent.recovery_id,
        detail={
            "succession_id": observation.succession_id,
            "grant_sha256": grant.sha256(),
            "predecessor_sha256": owner.sha256(),
            "original_job_id": row.original_job_id,
        },
    )
    await session.flush()
    return grant, True


async def verify_live_succession_grant(
    session: AsyncSession,
    succession_id: str,
    grant_sha256: str,
    process: NodeAgentProcessIdentity,
    node: str,
) -> UpgradeExpiredRecoverySuccessionGrant:
    """Fresh initial handover gate, never a claim or physical lease.

    Authenticated fixed-node transport is required at the eventual caller. Actual
    current process/verified bytes, same kernel, preserved Source and exclusive
    private lifetime capability must be checked separately before every effect.
    Grant expiry is final for initial handover: only a separately retained accepted
    successor claim can later resume within the unchanged root recovery budget.
    """
    await operation_lock(session)
    entry = await session.get(UpgradeRecoverySuccessionRow, succession_id, with_for_update=True)
    if entry is None:
        raise UpgradeUnavailable("Succession grant is absent")
    grant = UpgradeExpiredRecoverySuccessionGrant.model_validate(entry.grant_payload)
    proof = grant.observation
    owner = proof.previous_owner
    intent = owner.consumption.transfer.intent
    if (
        entry.grant_sha256 != grant.sha256()
        or grant.sha256() != grant_sha256
        or proof.succession_id != succession_id
        or entry.recovery_id != intent.recovery_id
        or entry.predecessor_sha256 != owner.sha256()
        or process != proof.successor
    ):
        raise UpgradeForbidden()
    row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id, with_for_update=True)
    if row is None:
        raise UpgradeUnavailable("Succession original authority is absent")
    await verify_binding(session, row, owner, node)
    if (row.user_id, row.key_id, row.credential_version) != (
        grant.user_id,
        grant.key_id,
        grant.credential_version,
    ):
        raise UpgradeForbidden()
    if row.owner_claim_payload != owner.model_dump(mode="json"):
        raise UpgradeConflict("Succession retained predecessor changed")
    now = datetime.now(UTC)
    if (
        row.active_slot != 1
        or row.phase not in {"recovering", "verifying"}
        or not grant.accepted_at <= now < grant.expires_at <= intent.deadline_at
    ):
        raise UpgradeUnavailable("Succession initial handover is no longer live")
    return grant
