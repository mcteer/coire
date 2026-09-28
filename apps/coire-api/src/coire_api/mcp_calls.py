"""Owner-scoped persistence for MCP tool calls and retained results."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import McpCallRow
from coire_core.models.mcp import (
    ApplyInput,
    ApplyResult,
    McpCall,
    McpCallState,
    McpToolName,
    PlanInput,
    PlanResult,
    ResearchInput,
    ResearchResult,
    WorkspaceSource,
)


class McpCallNotFound(LookupError):
    """The call is absent or does not belong to the current user."""


class McpCallConflict(ValueError):
    """The call cannot move to the requested state."""


def require_mcp_owner(principal: Principal) -> uuid.UUID:
    if (
        principal.kind is not PrincipalKind.API_KEY
        or "mcp" not in principal.scopes
        or principal.user_id is None
    ):
        raise PermissionError("MCP requires a user-bound API key with mcp scope")
    return principal.user_id


def project_call(row: McpCallRow) -> McpCall:
    return McpCall(
        id=row.id,
        tool=row.tool,
        owner_user_id=row.owner_user_id,
        credential_id=row.credential_id,
        source=WorkspaceSource.model_validate(row.source),
        model_id=row.model_id,
        run_id=row.run_id,
        state=row.state,
        failure_code=row.failure_code,
        requested_at=row.requested_at,
        finished_at=row.finished_at,
    )


async def create_call(
    session: AsyncSession,
    *,
    principal: Principal,
    tool: McpToolName,
    source: WorkspaceSource,
    model_id: uuid.UUID,
    input: ResearchInput | PlanInput | ApplyInput,
    task: str,
) -> McpCallRow:
    owner_id = require_mcp_owner(principal)
    if (
        (tool is McpToolName.RESEARCH and not isinstance(input, ResearchInput))
        or (tool is McpToolName.PLAN and not isinstance(input, PlanInput))
        or (tool is McpToolName.APPLY and not isinstance(input, ApplyInput))
        or input.source != source
    ):
        raise McpCallConflict("tool input and source do not match call")
    if not task or len(task) > 100_000:
        raise McpCallConflict("MCP task must contain 1 to 100000 characters")
    row = McpCallRow(
        tool=tool,
        owner_user_id=owner_id,
        credential_id=principal.api_key_id,
        source=source.model_dump(mode="json"),
        input=input.model_dump(mode="json"),
        task=task,
        model_id=model_id,
        state=McpCallState.ACCEPTED,
    )
    session.add(row)
    await session.flush()
    return row


async def get_owned_call(
    session: AsyncSession, *, call_id: uuid.UUID, principal: Principal
) -> McpCallRow:
    owner_id = require_mcp_owner(principal)
    row = await session.scalar(
        select(McpCallRow).where(McpCallRow.id == call_id, McpCallRow.owner_user_id == owner_id)
    )
    if row is None:
        raise McpCallNotFound(str(call_id))
    return row


async def attach_run(session: AsyncSession, *, row: McpCallRow, run_id: uuid.UUID) -> None:
    if row.state not in {McpCallState.ACCEPTED, McpCallState.PREPARING} or row.run_id is not None:
        raise McpCallConflict("call is already assigned to a run")
    row.run_id = run_id
    row.state = McpCallState.QUEUED
    await session.flush()


async def store_result(
    session: AsyncSession,
    *,
    row: McpCallRow,
    result: ResearchResult | PlanResult | ApplyResult,
) -> None:
    if row.state in {
        McpCallState.SUCCEEDED,
        McpCallState.FAILED,
        McpCallState.TIMED_OUT,
        McpCallState.CANCELLED,
    }:
        raise McpCallConflict("call is already terminal")
    if row.run_id != result.run_id:
        raise McpCallConflict("result run does not match call")
    result_tool = (
        McpToolName.RESEARCH
        if isinstance(result, ResearchResult)
        else McpToolName.PLAN
        if isinstance(result, PlanResult)
        else McpToolName.APPLY
    )
    if row.tool is not result_tool:
        raise McpCallConflict("result type does not match tool")
    row.result = result.model_dump(mode="json")
    row.state = McpCallState.SUCCEEDED
    row.finished_at = datetime.now(UTC)
    await session.flush()


async def fail_call(
    session: AsyncSession, *, row: McpCallRow, state: McpCallState, code: str
) -> None:
    if state not in {McpCallState.FAILED, McpCallState.TIMED_OUT, McpCallState.CANCELLED}:
        raise ValueError("failure state must be terminal")
    if row.state is McpCallState.SUCCEEDED:
        raise McpCallConflict("successful call cannot fail")
    row.state = state
    row.failure_code = code[:64]
    row.finished_at = datetime.now(UTC)
    await session.flush()


async def get_owned_result(
    session: AsyncSession,
    *,
    call_id: uuid.UUID,
    principal: Principal,
    expected_type: type[ResearchResult] | type[PlanResult] | type[ApplyResult],
) -> ResearchResult | PlanResult | ApplyResult:
    row = await get_owned_call(session, call_id=call_id, principal=principal)
    if row.state is not McpCallState.SUCCEEDED or row.result is None:
        raise McpCallConflict("result is not available")
    expected_tool = (
        McpToolName.RESEARCH
        if expected_type is ResearchResult
        else McpToolName.PLAN
        if expected_type is PlanResult
        else McpToolName.APPLY
    )
    if expected_tool is not row.tool:
        raise McpCallConflict("result type does not match tool")
    return expected_type.model_validate(row.result)
