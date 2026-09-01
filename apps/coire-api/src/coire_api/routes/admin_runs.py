"""Administrative run listing and kill switch."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status

from coire_api import runs
from coire_api.audit import write_principal_audit
from coire_api.auth import CurrentAdmin
from coire_api.deps import SessionDep
from coire_api.run_tokens import revoke_run_token
from coire_core.models.runs import AgentRun, AgentRunState, RunKillRequest

router = APIRouter(prefix="/api/v1/admin/runs", tags=["admin: runs"])


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
    try:
        row = await runs.get_visible_run(session, run_id, principal)
        await revoke_run_token(session, run_id)
        await runs.transition(session, row, AgentRunState.KILL_REQUESTED, body.reason)
    except runs.RunNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such run") from exc
    except runs.RunConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
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
    return await runs.project_run(session, row)
