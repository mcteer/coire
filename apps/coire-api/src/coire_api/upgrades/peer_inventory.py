"""A native guard observes its fenced peer without receiving the peer's credential."""

from datetime import UTC, datetime

from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.nodes_client import NodeClient, NodeError
from coire_api.upgrades.grants import verify_fenced
from coire_api.upgrades.persistence import get_operation
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    TERMINAL_UPGRADE_PHASES,
    NodeUpgradeInventory,
    NodeUpgradePeerRead,
    StudioName,
    UpgradeExecutionPlan,
    UpgradeKind,
    UpgradeOperation,
)

tracer = trace.get_tracer("coire.api.upgrades")


@tracer.start_as_current_span(
    "coire.api.upgrade_peer_inventory", record_exception=False, set_status_on_exception=False
)
async def guard_peer_inventory(
    session: AsyncSession,
    requester: StudioName,
    job_id: str,
    request: NodeUpgradePeerRead,
    client: NodeClient,
) -> NodeUpgradeInventory:
    async def authority() -> UpgradeExecutionPlan:
        row = await get_operation(session, job_id)
        if row.execution_plan is None:
            raise UpgradeConflict("Native peer observation has no accepted plan")
        try:
            operation = UpgradeOperation.model_validate(row.payload)
            plan = UpgradeExecutionPlan.model_validate(row.execution_plan)
        except ValueError:
            raise UpgradeUnavailable(
                "Native peer observation authority cannot be verified"
            ) from None
        from coire_api.upgrades.recovery_support_scope import require_original

        require_original(
            plan.operation.job_id, plan.operation.generation, plan.source.manifest_sha256
        )
        if (
            operation.kind is not UpgradeKind.STUDIO
            or operation.node != requester
            or operation.phase in TERMINAL_UPGRADE_PHASES
            or (operation.generation, plan.authority_sha256())
            != (request.generation, request.plan_sha256)
            or plan.operation.authority_sha256() != operation.authority_sha256()
            or plan.peer_runtime_sha256 is None
        ):
            raise UpgradeConflict("Native peer observation owner differs")
        await verify_fenced(session, plan)
        return plan

    plan = await authority()
    peer = "coire-edge-b" if requester == "coire-edge-a" else "coire-edge-a"
    if peer not in plan.maintenance.targets:
        raise UpgradeConflict("Native peer is outside accepted maintenance")
    try:
        snapshot = await client.upgrade_inventory(peer, job_id, request.generation)
    except NodeError:
        raise UpgradeUnavailable("Fenced native peer cannot be observed") from None
    if (snapshot.job_id, snapshot.generation, snapshot.node, snapshot.runtime_sha256) != (
        job_id,
        request.generation,
        peer,
        plan.peer_runtime_sha256,
    ) or not 0 <= (datetime.now(UTC) - snapshot.observed_at).total_seconds() <= 5:
        raise UpgradeUnavailable("Fenced native peer identity or freshness differs")
    current = await authority()
    if current.authority_sha256() != plan.authority_sha256():
        raise UpgradeConflict("Native peer observation authority changed")
    return snapshot
