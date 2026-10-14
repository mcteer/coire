"""Read the fenced rank-zero cleanup peer without granting probe authority."""

from datetime import UTC, datetime

from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.nodes_client import NodeClient, NodeError
from coire_api.upgrades.grants import verify_fenced
from coire_api.upgrades.persistence import get_operation
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    TERMINAL_UPGRADE_PHASES,
    NodeUpgradeCollectiveCleanup,
    NodeUpgradeInventory,
    StudioName,
    UpgradeExecutionPlan,
    UpgradeKind,
    UpgradeOperation,
)

tracer = trace.get_tracer("coire.api.upgrades")


@tracer.start_as_current_span(
    "coire.api.collective.cleanup_peer", record_exception=False, set_status_on_exception=False
)
async def collective_cleanup_peer(
    session: AsyncSession,
    requester: StudioName,
    job_id: str,
    body: NodeUpgradeCollectiveCleanup,
    client: NodeClient,
) -> NodeUpgradeInventory:
    """A separate cleanup read; existing qualification peer policy stays intact."""
    body = NodeUpgradeCollectiveCleanup.model_validate(body.model_dump(mode="json"))
    scope = body.scope

    async def authority() -> UpgradeExecutionPlan:
        row = await get_operation(session, job_id)
        if row.execution_plan is None:
            raise UpgradeConflict("Collective cleanup has no accepted plan")
        try:
            operation = UpgradeOperation.model_validate(row.payload)
            plan = UpgradeExecutionPlan.model_validate(row.execution_plan)
        except ValueError:
            raise UpgradeUnavailable("Collective cleanup authority cannot be verified") from None
        if (
            requester != "coire-edge-a"
            or operation.kind is not UpgradeKind.STUDIO
            or operation.phase in TERMINAL_UPGRADE_PHASES
            or job_id != scope.plan.operation.job_id
            or operation.generation != scope.request.generation
            or operation.authority_sha256() != plan.operation.authority_sha256()
            or plan.authority_sha256() != scope.request.plan_sha256
            or plan.authority_sha256() != scope.plan.authority_sha256()
            or set(plan.maintenance.targets) != {"coire-edge-a", "coire-edge-b"}
        ):
            raise UpgradeConflict("Collective cleanup accepted owner differs")
        await verify_fenced(session, plan)
        return plan

    plan = await authority()
    # Do not hold a database transaction across peer I/O; recheck afterwards.
    await session.commit()
    try:
        snapshot = await client.upgrade_inventory("coire-edge-b", job_id, scope.request.generation)
    except NodeError:
        raise UpgradeUnavailable("Collective cleanup peer cannot be observed") from None
    runtimes = {scope.runtime_pair()[1]}
    if plan.operation.node == "coire-edge-b":
        source = plan.source.manifest.environment_sha256
        if source is None:
            raise UpgradeUnavailable("Collective cleanup source runtime is unavailable")
        runtimes.add(source)
    if (
        (snapshot.job_id, snapshot.generation, snapshot.node)
        != (job_id, scope.request.generation, "coire-edge-b")
        or snapshot.runtime_sha256 not in runtimes
        or not 0 <= (datetime.now(UTC) - snapshot.observed_at).total_seconds() <= 5
    ):
        raise UpgradeUnavailable("Collective cleanup peer identity or freshness differs")
    current = await authority()
    if current.authority_sha256() != plan.authority_sha256():
        raise UpgradeConflict("Collective cleanup authority changed during observation")
    return snapshot
