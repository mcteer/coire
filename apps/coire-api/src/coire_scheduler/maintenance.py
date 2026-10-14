"""Persistent maintenance barriers sharing placement's ordered admission locks."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, true
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import NodeRow, UpgradeMaintenanceRow
from coire_api.upgrades.persistence import get_operation, get_release, operation_lock
from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import (
    MaintenanceTarget,
    ServiceName,
    StudioName,
    UpgradeExecutionPlan,
    UpgradeKind,
    UpgradeMaintenance,
    UpgradeOperation,
)


def dependency_scope(
    kind: UpgradeKind,
    *,
    node: StudioName | None,
    cohort: tuple[ServiceName, ...] = (),
    schema_changed: bool = False,
    paired: bool = False,
) -> tuple[MaintenanceTarget, ...]:
    """Fixed release dependencies determine drain; callers cannot supply arbitrary scope."""
    if kind is UpgradeKind.STUDIO:
        if node is None or cohort:
            raise UpgradeConflict("Studio maintenance scope differs")
        return ("coire-edge-a", "coire-edge-b") if paired else (node,)
    if node is not None or (not cohort and not schema_changed):
        raise UpgradeConflict("Core maintenance scope differs")
    if schema_changed or {"coire-api", "coire-scheduler", "coire-web"}.intersection(cohort):
        return (*cohort, "core", "coire-edge-a", "coire-edge-b")
    return cohort


async def allows_admission(session: AsyncSession, node_ids: Sequence[UUID]) -> bool:
    """Caller holds the ordered node locks until admission commits; renewals bypass this."""
    from coire_api.upgrades.gateway_smoke_scope import smoke_admission_exemption

    smoke = await smoke_admission_exemption(session, node_ids)
    if smoke is not None:
        return smoke
    names = list(
        (await session.scalars(select(NodeRow.name).where(NodeRow.id.in_(node_ids)))).all()
    )
    if len(names) != len(set(node_ids)):
        return False
    return (
        await session.scalar(
            select(UpgradeMaintenanceRow.target)
            .where(
                UpgradeMaintenanceRow.target.in_(["cluster", *names]),
                UpgradeMaintenanceRow.released.is_(False),
            )
            .limit(1)
        )
        is None
    )


async def fence(session: AsyncSession, value: UpgradeMaintenance) -> None:
    from coire_api.placement.service import lock_nodes_for_admission

    await operation_lock(session)
    operation_row = await get_operation(session, value.job_id, lock=True)
    operation = UpgradeOperation.model_validate(operation_row.payload)
    if operation.generation != value.generation or operation.finished_at is not None:
        raise UpgradeConflict("Maintenance operation is stale or terminal")
    if value.state == "released":
        raise UpgradeConflict("Fence establishment cannot release maintenance")
    source = await get_release(session, operation.source_release_id)
    candidate = await get_release(session, operation.target_release_id)
    plan = (
        UpgradeExecutionPlan.model_validate(operation_row.execution_plan)
        if operation_row.execution_plan is not None
        else None
    )
    if plan is not None and plan.operation.authority_sha256() != operation.authority_sha256():
        raise UpgradeConflict("Maintenance plan differs from accepted operation")
    expected_scope = dependency_scope(
        operation.kind,
        node=operation.node,
        cohort=operation.cohort,
        schema_changed=(
            plan.needs_migration
            if plan is not None
            else source.manifest.schema_head != candidate.manifest.schema_head
        ),
        paired=source.manifest.paired_required or candidate.manifest.paired_required,
    )
    if set(value.targets) != set(expected_scope) or (
        value.initiator_user_id is not None and value.initiator_user_id != operation.user_id
    ):
        raise UpgradeConflict("Maintenance scope differs from accepted release dependencies")
    nodes = list(
        await session.scalars(
            select(NodeRow.id).where(
                true() if "cluster" in value.targets else NodeRow.name.in_(value.targets)
            )
        )
    )
    await lock_nodes_for_admission(session, nodes)
    for target in sorted(value.targets):
        row = await session.get(UpgradeMaintenanceRow, target, with_for_update=True)
        if row is not None:
            if row.generation > value.generation:
                raise UpgradeConflict("Maintenance generation is stale")
            if not row.released and (row.job_id, row.generation) != (
                value.job_id,
                value.generation,
            ):
                raise UpgradeConflict("Another operation retains maintenance")
            if row.released and row.generation == value.generation:
                raise UpgradeConflict("Released maintenance cannot become active again")
            row.job_id = value.job_id
            row.generation = value.generation
            row.released = False
            row.payload = value.model_dump(mode="json")
        else:
            session.add(
                UpgradeMaintenanceRow(
                    target=target,
                    job_id=value.job_id,
                    generation=value.generation,
                    released=False,
                    payload=value.model_dump(mode="json"),
                )
            )
    await session.flush()


async def release_verified(session: AsyncSession, job_id: str, generation: int) -> None:
    """Only persisted terminal verification can reopen admissions; no timer can do so."""
    await operation_lock(session)
    operation = UpgradeOperation.model_validate(
        (await get_operation(session, job_id, lock=True)).payload
    )
    if operation.generation != generation or operation.finished_at is None:
        raise UpgradeConflict("Maintenance requires verified terminal reconciliation")
    rows = await session.scalars(
        select(UpgradeMaintenanceRow)
        .where(
            UpgradeMaintenanceRow.job_id == job_id,
            UpgradeMaintenanceRow.generation == generation,
        )
        .with_for_update()
    )
    for row in rows:
        value = UpgradeMaintenance.model_validate(row.payload)
        row.released = True
        row.payload = value.model_copy(
            update={"state": "released", "updated_at": datetime.now(UTC)}
        ).model_dump(mode="json")


async def allows_core_work(session: AsyncSession, services: tuple[ServiceName, ...]) -> bool:
    """Hold the fence-establishment lock through the caller's durable new-work claim.

    Existing work, its renewals and cleanup bypass this gate. Call before actor,
    quota or job row locks so all admissions use the same lock ordering.
    """
    if not services:
        raise ValueError("Core work requires a fixed affected service scope")
    await operation_lock(session)
    from coire_api.upgrades.gateway_smoke_scope import smoke_core_work_exemption

    smoke = await smoke_core_work_exemption(session, services)
    if smoke is not None:
        return smoke
    return (
        await session.scalar(
            select(UpgradeMaintenanceRow.target)
            .where(
                UpgradeMaintenanceRow.target.in_(("cluster", "core", *services)),
                UpgradeMaintenanceRow.released.is_(False),
            )
            .limit(1)
        )
        is None
    )
