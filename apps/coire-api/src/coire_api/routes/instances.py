"""Authenticated model-instance lifecycle and event-stream routes."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from coire_api.audit import write_audit
from coire_api.auth import CurrentAuthenticated
from coire_api.db import (
    InstanceTransitionRow,
    ModelInstanceRow,
    ModelRow,
    ModelVariantRow,
    NodeMemoryLedgerRow,
    NodeRow,
    session_scope,
)
from coire_api.deps import SessionDep, SettingsDep
from coire_api.instance import service
from coire_api.placement.service import project_ledgers
from coire_api.sharding import link_projection
from coire_core.models.instance import (
    TERMINAL_INSTANCE_STATES,
    ClusterNodeState,
    ClusterState,
    InstanceCreate,
    InstanceState,
    ModelInstance,
)
from coire_core.models.node import ThermalState

router = APIRouter(prefix="/api/v1", tags=["instances"])
ACTIVE_STATES = (
    InstanceState.REQUESTED,
    InstanceState.RESERVING,
    InstanceState.LAUNCHING,
    InstanceState.WARMING,
)


@router.post("/instances", response_model=ModelInstance, status_code=status.HTTP_202_ACCEPTED)
async def create_instance(
    body: InstanceCreate,
    principal: CurrentAuthenticated,
    session: SessionDep,
) -> ModelInstance:
    model = await session.get(ModelRow, body.model_id)
    variant = await session.get(ModelVariantRow, body.variant_id)
    if model is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such model")
    if variant is None or variant.model_id != model.id or not variant.validated:
        raise HTTPException(status.HTTP_409_CONFLICT, "variant is not verified for this model")
    policy = body.policy or model.placement_policy
    # Serialize the cold-key lookup without forbidding multiple ready instances later.
    await session.execute(
        select(
            func.pg_advisory_xact_lock(func.hashtext(f"instance:{model.id}:{variant.id}:{policy}"))
        )
    )
    row = await session.scalar(
        select(ModelInstanceRow)
        .where(
            ModelInstanceRow.model_id == model.id,
            ModelInstanceRow.variant_id == variant.id,
            ModelInstanceRow.policy == policy,
            ModelInstanceRow.state.in_(ACTIVE_STATES),
        )
        .order_by(ModelInstanceRow.created_at)
        .limit(1)
    )
    if row is None:
        row = ModelInstanceRow(
            model_id=model.id,
            variant_id=variant.id,
            policy=policy,
            state=InstanceState.REQUESTED,
        )
        session.add(row)
        await session.flush()
        await service.append_initial_transition(session, row)
        await write_audit(
            session,
            actor=principal.subject or principal.kind.value,
            action="instance.create",
            target_type="model_instance",
            target_id=str(row.id),
            detail={"model_id": str(model.id), "variant_id": str(variant.id), "policy": policy},
        )
    await session.commit()
    await session.refresh(row)
    return await service.project_instance(session, row)


@router.get("/instances", response_model=list[ModelInstance])
async def list_instances(
    principal: CurrentAuthenticated, session: SessionDep
) -> list[ModelInstance]:
    rows = list(
        (
            await session.execute(
                select(ModelInstanceRow).order_by(ModelInstanceRow.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [await service.project_instance(session, row) for row in rows]


@router.get("/state", response_model=ClusterState)
async def cluster_state(
    principal: CurrentAuthenticated, session: SessionDep, settings: SettingsDep
) -> ClusterState:
    ledgers = await project_ledgers(session)
    node_rows = {row.id: row for row in (await session.execute(select(NodeRow))).scalars().all()}
    ledger_rows = {
        row.node_id: row
        for row in (await session.execute(select(NodeMemoryLedgerRow))).scalars().all()
    }
    instance_rows = list((await session.execute(select(ModelInstanceRow))).scalars().all())
    nodes: list[ClusterNodeState] = []
    for ledger in ledgers:
        row = node_rows[ledger.node_id]
        ledger_row = ledger_rows[ledger.node_id]
        try:
            thermal = ThermalState(ledger_row.thermal_state or "unknown")
        except ValueError:
            thermal = ThermalState.UNKNOWN
        nodes.append(
            ClusterNodeState(
                id=row.id,
                name=row.name,
                reachability=row.reachability,
                health_observed_at=row.health_observed_at or ledger.health_sampled_at,
                cpu_percent=ledger_row.cpu_percent,
                gpu_percent=row.gpu_percent,
                thermal_state=thermal,
                budget_bytes=ledger.budget_bytes,
                reserved_bytes=ledger.reserved_bytes,
                reservations=ledger.reservations,
            )
        )
    return ClusterState(
        observed_at=datetime.now(UTC),
        nodes=nodes,
        instances=[await service.project_instance(session, row) for row in instance_rows],
        studio_link=await link_projection(session, settings),
    )


@router.get("/instances/{instance_id}", response_model=ModelInstance)
async def get_instance(
    instance_id: uuid.UUID, principal: CurrentAuthenticated, session: SessionDep
) -> ModelInstance:
    row = await session.get(ModelInstanceRow, instance_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such instance")
    return await service.project_instance(session, row)


@router.delete(
    "/instances/{instance_id}", response_model=ModelInstance, status_code=status.HTTP_202_ACCEPTED
)
async def drain_instance(
    instance_id: uuid.UUID,
    principal: CurrentAuthenticated,
    session: SessionDep,
    settings: SettingsDep,
) -> ModelInstance:
    row = await session.get(ModelInstanceRow, instance_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such instance")
    if row.state is InstanceState.READY:
        row.drain_deadline = datetime.now(UTC) + timedelta(
            seconds=settings.instance_drain_timeout_s
        )
        row = await service.transition(session, instance_id, InstanceState.DRAINING)
        await write_audit(
            session,
            actor=principal.subject or principal.kind.value,
            action="instance.drain",
            target_type="model_instance",
            target_id=str(instance_id),
        )
        await session.commit()
    elif row.state not in {InstanceState.DRAINING, *TERMINAL_INSTANCE_STATES}:
        raise HTTPException(status.HTTP_409_CONFLICT, "instance is not ready to drain")
    return await service.project_instance(session, row)


@router.get("/instances/{instance_id}/events", response_class=StreamingResponse)
async def instance_events(
    instance_id: uuid.UUID,
    principal: CurrentAuthenticated,
    settings: SettingsDep,
    last_event_id: Annotated[int | None, Header(alias="Last-Event-ID", ge=0)] = None,
) -> StreamingResponse:
    async with session_scope() as session:
        if await session.get(ModelInstanceRow, instance_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such instance")

    async def stream() -> AsyncIterator[str]:
        cursor = last_event_id or 0
        current_only = last_event_id is None
        while True:
            async with session_scope() as session:
                query = select(InstanceTransitionRow).where(
                    InstanceTransitionRow.instance_id == instance_id,
                    InstanceTransitionRow.id > cursor,
                )
                query = (
                    query.order_by(InstanceTransitionRow.id.desc()).limit(1)
                    if current_only
                    else query.order_by(InstanceTransitionRow.id)
                )
                rows = list((await session.execute(query)).scalars().all())
                current_only = False
            for row in rows:
                cursor = row.id
                event = service.project_transition(row)
                yield (
                    f"id: {row.id}\nevent: instance.state\ndata: "
                    f"{json.dumps(event.model_dump(mode='json'))}\n\n"
                )
                if event.state in TERMINAL_INSTANCE_STATES:
                    return
            await asyncio.sleep(settings.instance_event_poll_interval_s)

    return StreamingResponse(stream(), media_type="text/event-stream")
