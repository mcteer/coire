"""Shared owner checks for Chat and MCP coding result references."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api import runs
from coire_api.audit import write_principal_audit
from coire_api.auth import Principal, require_owned_chat
from coire_api.db import (
    AgentRunRow,
    ChatTurnRow,
    McpArtifactRow,
    McpCallRow,
    RegisteredWorkspaceRow,
    RunCommandRow,
)
from coire_api.run_tokens import revoke_run_token
from coire_core.errors import ChatConflict, ChatNotFound
from coire_core.models.chat import ChatTurnCreate
from coire_core.models.mcp import (
    ApplyInput,
    ApplyResult,
    McpCallState,
    McpToolName,
    PlanInput,
    PlanResult,
    ResearchInput,
    ResearchResult,
    WorkspaceSource,
)
from coire_core.models.node import WorkspaceVisualInput
from coire_core.models.runs import (
    TERMINAL_RUN_STATES,
    AgentRunState,
    RunCommandState,
    RunOperation,
)


@dataclass(frozen=True, slots=True)
class CodingRequest:
    tool: McpToolName
    input: ResearchInput | PlanInput | ApplyInput
    task: str
    visual_inputs: tuple[WorkspaceVisualInput, ...] = ()


async def create_coding_call(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    credential_id: uuid.UUID | None,
    request: CodingRequest,
    model_id: uuid.UUID,
) -> McpCallRow:
    """Create the common durable call record after the caller's own auth gate."""
    if request.input.model_id is not None and request.input.model_id != model_id:
        raise ChatConflict("coding model selection changed")
    if not request.task or len(request.task) > 100_000:
        raise ChatConflict("coding task exceeds its limit")
    row = McpCallRow(
        tool=request.tool,
        owner_user_id=owner_user_id,
        credential_id=credential_id,
        source=request.input.source.model_dump(mode="json"),
        input={
            **request.input.model_dump(mode="json"),
            **(
                {
                    "coire_visual_inputs": [
                        item.model_dump(mode="json") for item in request.visual_inputs
                    ]
                }
                if request.visual_inputs
                else {}
            ),
        },
        task=request.task,
        model_id=model_id,
        state=McpCallState.ACCEPTED,
    )
    session.add(row)
    await session.flush()
    return row


async def request_coding_kill(
    session: AsyncSession,
    principal: Principal,
    run_id: uuid.UUID,
    *,
    reason: str,
    audit_action: str,
    expected_call_id: uuid.UUID | None = None,
) -> bool:
    """Revoke a user run before queuing its idempotent Studio kill."""
    row = await session.get(AgentRunRow, run_id, with_for_update=True)
    if (
        row is None
        or row.requester_user_id != principal.user_id
        or (expected_call_id is not None and row.prepared_request_id != expected_call_id)
    ):
        raise ChatNotFound()
    if row.state in TERMINAL_RUN_STATES:
        return row.state is AgentRunState.KILLED
    await revoke_run_token(session, run_id)
    if row.state is not AgentRunState.KILL_REQUESTED:
        await runs.transition(session, row, AgentRunState.KILL_REQUESTED, reason)
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
    else:
        await runs.transition(session, row, AgentRunState.KILLED, "run stopped before placement")
    from datetime import UTC, datetime

    row.killed_by = principal.user_id
    row.killed_at = datetime.now(UTC)
    await write_principal_audit(
        session,
        principal=principal,
        action=audit_action,
        target_type="agent_run",
        target_id=str(run_id),
        detail={"reason": reason},
    )
    return row.node_id is None


async def prepare_chat_coding_request(
    session: AsyncSession,
    principal: Principal,
    body: ChatTurnCreate,
) -> CodingRequest:
    """Bind a browser coding action to a registered source and prior typed result."""
    if principal.user_id is None or body.action == "chat" or body.workspace_id is None:
        raise ChatConflict("code mode requires a registered workspace")
    workspace = await session.scalar(
        select(RegisteredWorkspaceRow).where(
            RegisteredWorkspaceRow.id == body.workspace_id,
            RegisteredWorkspaceRow.owner_user_id == principal.user_id,
        )
    )
    if workspace is None:
        raise ChatNotFound()
    if any(selection.mode != "visual" for selection in body.attachments):
        raise ChatConflict("coding attachments must use visual mode")
    if body.action == "research":
        if body.plan_id is not None or body.research_id is not None:
            raise ChatConflict("research cannot reference a prior coding result")
        source = WorkspaceSource(
            workspace_id=body.workspace_id, revision=body.source_revision or "HEAD"
        )
        research_input = ResearchInput(source=source, question=body.content, model_id=body.model_id)
        return CodingRequest(McpToolName.RESEARCH, research_input, body.content)

    prior_id = body.research_id if body.action == "plan" else body.plan_id
    if body.action == "apply" and body.research_id is not None:
        raise ChatConflict("Apply requires a plan from this workspace")
    if body.action == "apply" and prior_id is None:
        raise ChatConflict("Apply requires an explicit prior plan")
    pinned_revision = body.source_revision or "HEAD"
    prior_result: ResearchResult | PlanResult | None = None
    if prior_id is not None:
        prior = await session.get(McpCallRow, prior_id)
        expected_tool = McpToolName.RESEARCH if body.action == "plan" else McpToolName.PLAN
        if (
            prior is None
            or prior.owner_user_id != principal.user_id
            or prior.tool is not expected_tool
            or prior.state is not McpCallState.SUCCEEDED
            or prior.result is None
            or prior.run_id is None
        ):
            raise ChatNotFound()
        try:
            prior_source = WorkspaceSource.model_validate(prior.source)
        except ValueError as exc:
            raise ChatConflict("prior coding source is invalid") from exc
        if prior_source.workspace_id != body.workspace_id:
            raise ChatConflict("prior result belongs to another workspace")
        try:
            prior_result = (
                ResearchResult.model_validate(prior.result)
                if body.action == "plan"
                else PlanResult.model_validate(prior.result)
            )
        except ValueError as exc:
            raise ChatConflict("prior coding result is invalid") from exc
        if prior_result.run_id != prior.run_id:
            raise ChatConflict("prior result run does not match its call")
        if (
            body.source_revision is not None
            and body.source_revision != prior_result.source_revision
        ):
            raise ChatConflict("source revision differs from the prior result")
        pinned_revision = prior_result.source_revision
    source = WorkspaceSource(workspace_id=body.workspace_id, revision=pinned_revision)
    if body.action == "plan":
        plan_input = PlanInput(
            source=source,
            goal=body.content,
            research_result_id=body.research_id,
            model_id=body.model_id,
        )
        prior_context = (
            f"\nPrior research: {prior_result.model_dump_json()}"
            if isinstance(prior_result, ResearchResult)
            else ""
        )
        return CodingRequest(McpToolName.PLAN, plan_input, body.content + prior_context)
    assert isinstance(prior_result, PlanResult)
    apply_input = ApplyInput(source=source, plan_result_id=body.plan_id, model_id=body.model_id)
    return CodingRequest(McpToolName.APPLY, apply_input, prior_result.model_dump_json())


async def owned_chat_artifact_id(
    session: AsyncSession,
    principal: Principal,
    conversation_id: uuid.UUID,
    turn_id: uuid.UUID,
) -> uuid.UUID:
    """Resolve only a completed Apply artifact bound to this live owner turn."""
    await require_owned_chat(session, conversation_id, principal)
    turn = await session.get(ChatTurnRow, turn_id)
    if (
        turn is None
        or turn.conversation_id != conversation_id
        or turn.action != "apply"
        or turn.coding_call_id is None
        or turn.run_id is None
    ):
        raise ChatNotFound()
    call = await session.get(McpCallRow, turn.coding_call_id)
    if (
        call is None
        or call.id != turn.coding_call_id
        or call.owner_user_id != principal.user_id
        or call.tool is not McpToolName.APPLY
        or call.state is not McpCallState.SUCCEEDED
        or call.run_id != turn.run_id
        or call.result is None
    ):
        raise ChatNotFound()
    try:
        result = ApplyResult.model_validate(call.result)
    except ValueError as exc:
        raise ChatNotFound() from exc
    if result.run_id != turn.run_id:
        raise ChatNotFound()
    artifact = await session.get(McpArtifactRow, result.artifact_id)
    if (
        artifact is None
        or artifact.call_id != call.id
        or artifact.run_id != turn.run_id
        or artifact.owner_user_id != principal.user_id
    ):
        raise ChatNotFound()
    return result.artifact_id
