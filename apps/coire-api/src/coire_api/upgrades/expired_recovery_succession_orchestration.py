"""Trusted bounded same-kernel observer orchestration, never caller-supplied proof."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coire_api.auth import Principal
from coire_api.db import UpgradeExpiredRecoveryRow, UpgradeRecoverySuccessionRow
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.expired_recovery_observation_relay import SuccessionObservationRelay
from coire_api.upgrades.expired_recovery_owner import verify_live_owner
from coire_api.upgrades.expired_recovery_succession import accept_succession
from coire_api.upgrades.persistence import operation_lock
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoverySuccessionGrant,
    UpgradeExpiredRecoverySuccessionObservation,
)

Observer = Callable[
    [UpgradeExpiredRecoveryOwnerClaim, str], Awaitable[UpgradeExpiredRecoverySuccessionObservation]
]
tracer = trace.get_tracer("coire.api.recovery_succession")


async def accept_with_trusted_relay(
    sessions: async_sessionmaker[AsyncSession],
    principal: Principal,
    previous: UpgradeExpiredRecoveryOwnerClaim,
    succession_id: str,
    relay: "SuccessionObservationRelay",
) -> tuple[UpgradeExpiredRecoverySuccessionGrant, bool]:
    """Application-installed relay only; admin route stays unexposed until qualified."""

    async def observe(
        owner: UpgradeExpiredRecoveryOwnerClaim, identity: str
    ) -> UpgradeExpiredRecoverySuccessionObservation:
        return relay.read(owner, identity)

    return await accept_with_successor_observation(
        sessions, principal, previous, succession_id, observe
    )


async def accept_with_successor_observation(
    sessions: async_sessionmaker[AsyncSession],
    principal: Principal,
    previous: UpgradeExpiredRecoveryOwnerClaim,
    succession_id: str,
    observe: Observer,
) -> tuple[UpgradeExpiredRecoverySuccessionGrant, bool]:
    """Only an application-installed authenticated fixed-destination producer.

    Do not expose arbitrary caller snapshots or observer destinations. Commit the
    snapshot before bounded physical I/O; audited acceptance repeats all authority
    checks. Replay retains epochs and never repeats observation or native effects.
    """
    previous = UpgradeExpiredRecoveryOwnerClaim.model_validate_json(previous.model_dump_json())
    intent = previous.consumption.transfer.intent
    node = previous.consumption.transfer.legacy_guard.node
    with tracer.start_as_current_span("coire.api.recovery_succession_accept"):
        async with sessions.begin() as session:
            await operation_lock(session)
            await authorize(session, principal)
            existing = await session.get(UpgradeRecoverySuccessionRow, succession_id)
            if existing is not None:
                grant = UpgradeExpiredRecoverySuccessionGrant.model_validate(existing.grant_payload)
                if (
                    grant.observation.previous_owner != previous
                    or grant.sha256() != existing.grant_sha256
                ):
                    raise UpgradeConflict("Succession replay scope differs")
                if (principal.user_id, principal.api_key_id, principal.credential_version) != (
                    grant.user_id,
                    grant.key_id,
                    grant.credential_version,
                ):
                    raise UpgradeForbidden()
                return await accept_succession(session, principal, grant.observation, node)
            parent = await session.get(
                UpgradeExpiredRecoveryRow, intent.recovery_id, with_for_update=True
            )
            if parent is None or parent.owner_claim_payload != previous.model_dump(mode="json"):
                raise UpgradeConflict("Succession snapshot predecessor differs")
            if (principal.user_id, principal.api_key_id, principal.credential_version) != (
                parent.user_id,
                parent.key_id,
                parent.credential_version,
            ):
                raise UpgradeForbidden()
            await verify_live_owner(
                session, intent.recovery_id, previous.sha256(), previous.process, node
            )
        remaining = (intent.deadline_at - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise UpgradeUnavailable("Succession original recovery budget expired")
        try:
            async with asyncio.timeout(min(5.0, remaining)):
                observation = await observe(previous, succession_id)
        except TimeoutError:
            raise UpgradeUnavailable("Succession trusted observation budget exhausted") from None
        observation = UpgradeExpiredRecoverySuccessionObservation.model_validate_json(
            observation.model_dump_json()
        )
        if observation.previous_owner != previous or observation.succession_id != succession_id:
            raise UpgradeConflict("Succession trusted observer scope differs")
        async with sessions.begin() as session:
            return await accept_succession(session, principal, observation, node)
