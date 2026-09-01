"""Administrative run listing and kill switch."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from opentelemetry import metrics, trace

from coire_api import runs
from coire_api.audit import write_principal_audit
from coire_api.auth import CurrentAdmin
from coire_api.db import RunCommandRow
from coire_api.deps import SessionDep
from coire_api.run_tokens import revoke_run_token
from coire_core.models.runs import (
    AgentRun,
    AgentRunState,
    RunCommandState,
    RunKillRequest,
    RunOperation,
    RunProblemCode,
)

router = APIRouter(prefix="/api/v1/admin/runs", tags=["admin: runs"])
tracer = trace.get_tracer("coire.api.runs")
kills_total = metrics.get_meter("coire.api.runs").create_counter(
    "coire_api_run_kills_total", unit="1"
)


@router.get("", response_model=list[AgentRun])
async def list_runs(principal: CurrentAdmin, session: SessionDep) -> list[AgentRun]:
    return [
        await runs.project_run(session, row)
        for row in await runs.list_visible_runs(session, principal)
    ]


@router.delete("/{run_id}", response_model=AgentRun, status_code=status.HTTP_202_ACCEPTED)
async def kill_run(
    run_id: uuid.UUID,
    body: RunKillRequest,
    principal: CurrentAdmin,
    session: SessionDep,
) -> AgentRun:
    with tracer.start_as_current_span("coire.api.run.kill") as span:
        span.set_attribute("run_id", str(run_id))
        try:
            row = await runs.get_visible_run(session, run_id, principal)
            await revoke_run_token(session, run_id)
            await runs.transition(session, row, AgentRunState.KILL_REQUESTED, body.reason)
            if row.node_id is not None:
                command_id = runs.run_command_id(run_id, RunOperation.KILL)
                if await session.get(RunCommandRow, command_id) is None:
                    session.add(
                        RunCommandRow(
                            id=command_id,
                            run_id=run_id,
                            node_id=row.node_id,
                            operation=RunOperation.KILL,
                            attempt=1,
                            state=RunCommandState.PENDING,
                            detail={},
                        )
                    )
        except runs.RunNotFound as exc:
            kills_total.add(1, {"outcome": "not_found"})
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                {"code": RunProblemCode.NOT_FOUND.value, "detail": "no such run"},
            ) from exc
        except runs.RunConflict as exc:
            kills_total.add(1, {"outcome": "conflict"})
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {"code": RunProblemCode.CONFLICT.value, "detail": str(exc)},
            ) from exc
        row.killed_by = principal.user_id
        row.killed_at = datetime.now(UTC)
        await write_principal_audit(
            session,
            principal=principal,
            action="agent_run.kill",
            target_type="agent_run",
            target_id=str(run_id),
            detail={"reason": body.reason},
        )
        await session.commit()
        kills_total.add(1, {"outcome": "accepted"})
        return await runs.project_run(session, row)
