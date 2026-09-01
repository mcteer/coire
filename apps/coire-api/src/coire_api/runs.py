"""Authoritative AgentRun state and user/admin projections."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal
from coire_api.db import AgentRunRow, AgentRunTransitionRow, ModelRow, NodeRow
from coire_core.models.harness import ProfileName
from coire_core.models.registry import ModelState
from coire_core.models.runs import (
    TERMINAL_RUN_STATES,
    AgentRun,
    AgentRunCreate,
    AgentRunState,
    RunLimits,
    RunResourceUsage,
    RunTokenScope,
)


class RunNotFound(LookupError):
    pass


class RunConflict(ValueError):
    pass


async def project_run(session: AsyncSession, row: AgentRunRow) -> AgentRun:
    node = await session.get(NodeRow, row.node_id) if row.node_id else None
    return AgentRun(
        id=row.id,
        requester_user_id=row.requester_user_id,
        profile=ProfileName(row.profile),
        primary_model_id=row.primary_model_id,
        node_id=row.node_id,
        node_name=node.name if node else None,
        container_id=row.container_id,
        workspace_ref=row.workspace_ref,
        state=row.state,
        limits=RunLimits.model_validate(row.limits),
        exit_code=row.exit_code,
        failure_code=row.failure_code,
        failure_detail=row.failure_detail,
        result=row.result,
        resource_usage=RunResourceUsage.model_validate(row.resource_usage or {}),
        requested_at=row.requested_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        killed_by=row.killed_by,
        killed_at=row.killed_at,
    )


async def create_run(
    session: AsyncSession, request: AgentRunCreate, *, requester_user_id: uuid.UUID
) -> AgentRunRow:
    model = await session.get(ModelRow, request.primary_model_id)
    if model is None or model.state is not ModelState.READY:
        raise RunConflict("primary model must be a ready registry model")
    scope = RunTokenScope(
        permitted_model_ids=request.permitted_model_ids,
        permitted_tools=request.permitted_tools,
        spend_limit_tokens=request.spend_limit_tokens,
    )
    row = AgentRunRow(
        requester_user_id=requester_user_id,
        profile=request.profile.value,
        primary_model_id=request.primary_model_id,
        workspace_ref=request.workspace_ref,
        token_scope=scope.model_dump(mode="json"),
        state=AgentRunState.QUEUED,
        limits=request.limits.model_dump(mode="json"),
        resource_usage=RunResourceUsage().model_dump(mode="json"),
    )
    session.add(row)
    await session.flush()
    session.add(
        AgentRunTransitionRow(
            run_id=row.id,
            from_state=None,
            to_state=AgentRunState.QUEUED,
            reason="run requested",
        )
    )
    await session.flush()
    return row


async def get_visible_run(
    session: AsyncSession, run_id: uuid.UUID, principal: Principal
) -> AgentRunRow:
    row = await session.get(AgentRunRow, run_id)
    if row is None or (not principal.is_admin and row.requester_user_id != principal.user_id):
        raise RunNotFound(str(run_id))
    return row


async def list_visible_runs(session: AsyncSession, principal: Principal) -> list[AgentRunRow]:
    query = select(AgentRunRow).order_by(AgentRunRow.requested_at.desc())
    if not principal.is_admin:
        if principal.user_id is None:
            return []
        query = query.where(AgentRunRow.requester_user_id == principal.user_id)
    return list((await session.scalars(query)).all())


async def transition(
    session: AsyncSession,
    row: AgentRunRow,
    state: AgentRunState,
    reason: str,
) -> None:
    if row.state in TERMINAL_RUN_STATES:
        raise RunConflict(f"run is terminal ({row.state.value})")
    previous = row.state
    now = datetime.now(UTC)
    row.state = state
    row.updated_at = now
    if state is AgentRunState.RUNNING and row.started_at is None:
        row.started_at = now
    if state in TERMINAL_RUN_STATES:
        row.finished_at = now
    session.add(
        AgentRunTransitionRow(
            run_id=row.id,
            from_state=previous,
            to_state=state,
            reason=reason,
        )
    )
    await session.flush()
