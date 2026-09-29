"""Shared owner checks for Chat and MCP coding result references."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, require_owned_chat
from coire_api.db import ChatTurnRow, McpArtifactRow, McpCallRow
from coire_core.errors import ChatNotFound
from coire_core.models.mcp import ApplyResult, McpCallState, McpToolName


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
