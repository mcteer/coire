"""Task-local exemption for one exact registry engine owned by a Core upgrade."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import EngineProcessRow, ModelInstanceRow, NodeRow, UpgradeMaintenanceRow
from coire_api.upgrades.authorization import authorize
from coire_api.upgrades.executor import scoped_operation
from coire_api.upgrades.grants import initiating_actor
from coire_api.upgrades.service import smoke_asset
from coire_core.errors import UpgradeConflict
from coire_core.models.engine import EngineState
from coire_core.models.instance import InstanceState
from coire_core.models.upgrades import (
    CoreNodeSmokeScope,
    UpgradeMaintenance,
    UpgradeOperation,
    UpgradePhase,
)
from coire_core.settings import Settings


@dataclass(frozen=True)
class _ServingScope:
    body: CoreNodeSmokeScope
    principal: Principal
    settings: Settings
    instance_id: UUID
    restored: bool
    loading: bool


_scope: ContextVar[_ServingScope | None] = ContextVar("upgrade_gateway_serving", default=None)


def current_loading_smoke() -> CoreNodeSmokeScope | None:
    scope = _scope.get()
    return scope.body if scope is not None and scope.loading else None


@contextmanager
def gateway_smoke_scope(
    body: CoreNodeSmokeScope,
    principal: Principal,
    settings: Settings,
    instance_id: UUID,
    *,
    restored: bool,
    loading: bool = False,
) -> Iterator[None]:
    token = _scope.set(_ServingScope(body, principal, settings, instance_id, restored, loading))
    try:
        yield
    finally:
        _scope.reset(token)


async def smoke_admission_exemption(session: AsyncSession, node_ids: Sequence[UUID]) -> bool | None:
    scope = _scope.get()
    if scope is None:
        return None
    body, plan = scope.body, scope.body.plan
    if datetime.now(UTC) >= body.deadline_at:
        raise UpgradeConflict("Core smoke admission exceeded its original deadline")
    row, accepted = await scoped_operation(
        session, plan.operation.job_id, scope.principal, scope.settings
    )
    operation = UpgradeOperation.model_validate(row.payload)
    expected_phase = UpgradePhase.VERIFYING_SOURCE if scope.restored else UpgradePhase.VERIFYING
    if (
        accepted.authority_sha256() != plan.authority_sha256()
        or operation.phase is not expected_phase
    ):
        raise UpgradeConflict("Core smoke admission differs from its retained serving stage")
    if not scope.restored:
        if operation.cancel_requested:
            raise UpgradeConflict("Cancelled operation cannot admit Target serving smoke")
        await authorize(session, initiating_actor(row))
    engine = await session.get(EngineProcessRow, body.engine_id, populate_existing=True)
    instance = await session.get(ModelInstanceRow, scope.instance_id, populate_existing=True)
    if scope.loading and (
        instance is None
        or instance.upgrade_smoke_payload is None
        or CoreNodeSmokeScope.model_validate(instance.upgrade_smoke_payload) != body
    ):
        raise UpgradeConflict("Core smoke placement lacks its exact durable private instance")
    node = (
        await session.get(NodeRow, engine.node_id, populate_existing=True)
        if engine
        else await session.scalar(select(NodeRow).where(NodeRow.name == body.node))
    )
    allowed_instance_states = (
        {
            InstanceState.REQUESTED,
            InstanceState.RESERVING,
            InstanceState.LAUNCHING,
            InstanceState.WARMING,
            InstanceState.READY,
        }
        if scope.loading
        else {InstanceState.READY}
    )
    if (
        (engine is None and not scope.loading)
        or instance is None
        or node is None
        or set(node_ids) != {node.id}
        or node.name != body.node
        or node.runtime_sha256 != body.runtime_sha256
        or node.health_observed_at is None
        or not 0
        <= (datetime.now(UTC) - node.health_observed_at).total_seconds()
        <= scope.settings.placement_health_freshness_s
        or (
            engine is not None
            and (
                engine.state
                not in (
                    {EngineState.STARTING, EngineState.READY}
                    if scope.loading
                    else {EngineState.READY}
                )
                or engine.backend != "mlx_lm"
                or engine.instance_id != scope.instance_id
                or (engine.model_id, engine.variant_id, engine.adapter_id)
                != (plan.model_id, plan.model_variant_id, None)
            )
        )
        or instance.state not in allowed_instance_states
        or (instance.model_id, instance.variant_id, instance.adapter_id)
        != (plan.model_id, plan.model_variant_id, None)
    ):
        raise UpgradeConflict("Core smoke admission changed its exact Studio instance")
    asset = await smoke_asset(session, plan.model_id, (body.node,))
    if asset != (plan.model_id, plan.model_variant_id, plan.model_slug, plan.model_manifest_sha256):
        raise UpgradeConflict("Core smoke admission registry provenance changed")
    fences = (
        await session.scalars(
            select(UpgradeMaintenanceRow).where(
                UpgradeMaintenanceRow.target.in_(("cluster", body.node)),
                UpgradeMaintenanceRow.released.is_(False),
            )
        )
    ).all()
    for fence in fences:
        if (fence.job_id, fence.generation) != (
            plan.operation.job_id,
            plan.operation.generation,
        ) or not UpgradeMaintenance.model_validate(fence.payload).matches_plan(plan):
            raise UpgradeConflict("Another maintenance owner blocks Core serving smoke")
    if (
        body.node in plan.maintenance.targets or "cluster" in plan.maintenance.targets
    ) and not fences:
        raise UpgradeConflict("Core smoke admission lost its accepted node fence")
    return True


async def smoke_core_work_exemption(
    session: AsyncSession, services: tuple[str, ...]
) -> bool | None:
    scope = _scope.get()
    if scope is None:
        return None
    if not scope.loading or set(services) != {"coire-api", "coire-scheduler"}:
        raise UpgradeConflict("Core smoke exemption cannot admit another service concern")
    node = await session.scalar(select(NodeRow).where(NodeRow.name == scope.body.node))
    if node is None:
        raise UpgradeConflict("Core smoke placement lost its fixed Studio")
    await smoke_admission_exemption(session, [node.id])
    fences = (
        await session.scalars(
            select(UpgradeMaintenanceRow).where(
                UpgradeMaintenanceRow.target.in_(("cluster", "core", *services)),
                UpgradeMaintenanceRow.released.is_(False),
            )
        )
    ).all()
    for fence in fences:
        if (fence.job_id, fence.generation) != (
            scope.body.plan.operation.job_id,
            scope.body.plan.operation.generation,
        ) or not UpgradeMaintenance.model_validate(fence.payload).matches_plan(scope.body.plan):
            raise UpgradeConflict("Another Core maintenance owner blocks smoke placement")
    return True


@contextmanager
def placement_smoke_scope(
    body: CoreNodeSmokeScope, settings: Settings, instance_id: UUID
) -> Iterator[None]:
    principal = Principal(
        kind=PrincipalKind.UPGRADE_EXECUTOR,
        subject=settings.upgrade_executor_id,
        scopes=frozenset({"upgrades:execute"}),
    )
    with gateway_smoke_scope(
        body, principal, settings, instance_id, restored=body.check == "source", loading=True
    ):
        yield
