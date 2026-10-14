"""Fixed authenticated closure observation and peer release outside DB locks."""

import asyncio
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coire_api.nodes_client import NodeClient, NodeError
from coire_api.upgrades.expired_recovery_cold_resolution import (
    resolve_cold_recovery,
    retained_resolution,
    verify_closure_authority,
)
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdResolution,
    UpgradeExpiredRecoveryColdResolutionRequest,
)
from coire_core.models.upgrades import NodeMaintenanceCommand


async def resolve_with_observation(
    sessions: async_sessionmaker[AsyncSession],
    body: UpgradeExpiredRecoveryColdResolutionRequest,
    node: str,
    nodes: NodeClient,
) -> tuple[UpgradeExpiredRecoveryColdResolution, bool]:
    body = UpgradeExpiredRecoveryColdResolutionRequest.model_validate_json(body.model_dump_json())
    closure = body.closure
    owner = closure.handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
    intent = owner.intent
    plan = intent.original_plan
    async with sessions.begin() as session:
        previous = await retained_resolution(session, closure, node)
        if previous is not None:
            return previous, False
        await verify_closure_authority(session, body, node)
    try:
        async with asyncio.timeout(
            min(5.0, max(0.001, (intent.deadline_at - datetime.now(UTC)).total_seconds()))
        ):
            local = await nodes.upgrade_status(node)
            if (
                local.runtime_sha256 != owner.original_native.source.runtime.environment_sha256
                or local.release_sha256 != intent.request.expected_source_sha256
                or not local.bootstrap_ready
                or not 0 <= (datetime.now(UTC) - local.observed_at).total_seconds() <= 5
            ):
                raise UpgradeConflict("Cold resolution Source is not actually current and ready")
            peer_release = None
            if plan.peer_runtime_sha256 is not None:
                peer = "coire-edge-b" if node == "coire-edge-a" else "coire-edge-a"
                status = await nodes.upgrade_status(peer)
                if (
                    status.runtime_sha256 != plan.peer_runtime_sha256
                    or not status.bootstrap_ready
                    or not 0 <= (datetime.now(UTC) - status.observed_at).total_seconds() <= 5
                ):
                    raise UpgradeConflict("Cold resolution peer Source identity changed")
                # Recheck live authority immediately before the peer effect. Core
                # fencing and both slots remain held through a lost response.
                async with sessions.begin() as session:
                    previous = await retained_resolution(session, closure, node)
                    if previous is not None:
                        return previous, False
                    await verify_closure_authority(session, body, node)
                peer_release = await nodes.apply_upgrade_maintenance(
                    peer,
                    NodeMaintenanceCommand(
                        maintenance=plan.maintenance, action="release", terminal_verified=True
                    ),
                )
                if not peer_release.released:
                    raise UpgradeConflict("Cold resolution peer retained maintenance")
    except (NodeError, TimeoutError):
        raise UpgradeUnavailable("Cold resolution actual node closure is unavailable") from None
    async with sessions.begin() as session:
        return await resolve_cold_recovery(session, body, node, peer_release)
