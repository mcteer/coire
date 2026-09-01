"""Authenticated owner-visible AgentRun API."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from opentelemetry import metrics, trace
from sqlalchemy import select

from coire_api import runs
from coire_api.audit import write_principal_audit
from coire_api.auth import CurrentAuthenticated
from coire_api.db import AgentRunRow, AgentRunTransitionRow
from coire_api.deps import SessionDep
from coire_core.models.runs import (
    TERMINAL_RUN_STATES,
    AgentRun,
    AgentRunCreate,
    RunProblemCode,
)

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])
tracer = trace.get_tracer("coire.api.runs")
requests_total = metrics.get_meter("coire.api.runs").create_counter(
    "coire_api_run_requests_total", unit="1"
)


@router.post("", response_model=AgentRun, status_code=status.HTTP_202_ACCEPTED)
async def create_agent_run(
    body: AgentRunCreate, principal: CurrentAuthenticated, session: SessionDep
) -> AgentRun:
    with tracer.start_as_current_span("coire.api.run.create") as span:
        if principal.user_id is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "run requires a user identity")
        try:
            row = await runs.create_run(session, body, requester_user_id=principal.user_id)
        except runs.RunConflict as exc:
            requests_total.add(1, {"operation": "create", "outcome": "refused"})
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {"code": RunProblemCode.MODEL_UNAVAILABLE.value, "detail": str(exc)},
            ) from exc
        span.set_attribute("run_id", str(row.id))
        span.set_attribute("user_id", str(principal.user_id))
        await write_principal_audit(
            session,
            principal=principal,
            action="agent_run.create",
            target_type="agent_run",
            target_id=str(row.id),
            detail={"profile": row.profile, "primary_model_id": str(row.primary_model_id)},
        )
        await session.commit()
        requests_total.add(1, {"operation": "create", "outcome": "accepted"})
        return await runs.project_run(session, row)


@router.get("/{run_id}", response_model=AgentRun)
async def get_agent_run(
    run_id: uuid.UUID, principal: CurrentAuthenticated, session: SessionDep
) -> AgentRun:
    try:
        row = await runs.get_visible_run(session, run_id, principal)
    except runs.RunNotFound as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": RunProblemCode.NOT_FOUND.value, "detail": "no such run"},
        ) from exc
    return await runs.project_run(session, row)


@router.get("", response_model=list[AgentRun])
async def list_agent_runs(principal: CurrentAuthenticated, session: SessionDep) -> list[AgentRun]:
    return [
        await runs.project_run(session, row)
        for row in await runs.list_visible_runs(session, principal)
    ]


@router.get("/{run_id}/events")
async def run_events(
    run_id: uuid.UUID, principal: CurrentAuthenticated, session: SessionDep
) -> StreamingResponse:
    try:
        await runs.get_visible_run(session, run_id, principal)
    except runs.RunNotFound as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": RunProblemCode.NOT_FOUND.value, "detail": "no such run"},
        ) from exc

    async def events() -> AsyncIterator[bytes]:
        seen: set[uuid.UUID] = set()
        while True:
            query = (
                select(AgentRunTransitionRow)
                .where(AgentRunTransitionRow.run_id == run_id)
                .order_by(AgentRunTransitionRow.occurred_at, AgentRunTransitionRow.id)
            )
            rows = list((await session.scalars(query)).all())
            for row in rows:
                if row.id in seen:
                    continue
                seen.add(row.id)
                payload = {
                    "run_id": str(run_id),
                    "state": row.to_state.value,
                    "reason": row.reason,
                    "occurred_at": row.occurred_at.isoformat(),
                }
                yield f"data: {json.dumps(payload)}\n\n".encode()
            run = await session.get(AgentRunRow, run_id)
            if run is None or run.state in TERMINAL_RUN_STATES:
                return
            yield b": keep-alive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
    )
