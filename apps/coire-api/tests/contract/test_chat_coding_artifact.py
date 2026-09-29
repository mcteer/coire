"""Chat branch downloads bind a completed Apply result to the live owner turn."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import cast

import pytest
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.app import create_app
from coire_api.auth import Principal, PrincipalKind
from coire_api.coding_calls import owned_chat_artifact_id
from coire_api.db import ChatConversationRow, ChatTurnRow, McpArtifactRow, McpCallRow
from coire_api.routes import chat as chat_routes
from coire_core.errors import ChatNotFound
from coire_core.models.mcp import (
    ApplyResult,
    McpCallState,
    McpToolName,
)
from coire_core.models.mcp import (
    TestStatus as ResultTestStatus,
)
from coire_core.models.mcp import (
    TestSummary as ResultTestSummary,
)
from coire_core.settings import Settings


class Rows:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.owner_id, self.run_id, self.call_id, self.artifact_id = (
            uuid.uuid4() for _ in range(4)
        )
        self.conversation = ChatConversationRow(
            id=uuid.uuid4(),
            owner_user_id=self.owner_id,
            mode="code",
            title="Private",
            revision=1,
            created_at=now,
            updated_at=now,
        )
        self.turn = ChatTurnRow(
            id=uuid.uuid4(),
            conversation_id=self.conversation.id,
            action="apply",
            run_id=self.run_id,
            coding_call_id=self.call_id,
        )
        result = ApplyResult(
            run_id=self.run_id,
            branch="coire/feature",
            base_revision="a" * 40,
            head_revision="b" * 40,
            diff_excerpt="",
            diff_truncated=False,
            tests=ResultTestSummary(status=ResultTestStatus.PASSED),
            artifact_id=self.artifact_id,
        )
        self.call = McpCallRow(
            id=self.call_id,
            owner_user_id=self.owner_id,
            tool=McpToolName.APPLY,
            state=McpCallState.SUCCEEDED,
            run_id=self.run_id,
            result=result.model_dump(mode="json"),
        )
        self.artifact = McpArtifactRow(
            id=self.artifact_id,
            owner_user_id=self.owner_id,
            call_id=self.call_id,
            run_id=self.run_id,
            storage_ref="edge-a",
            sha256="c" * 64,
            size_bytes=10,
            collected_at=now,
            expires_at=now,
        )

    async def get(self, model: type, _id: uuid.UUID) -> object:
        if model is ChatConversationRow:
            return self.conversation if _id == self.conversation.id else None
        if model is ChatTurnRow:
            return self.turn if _id == self.turn.id else None
        if model is McpCallRow:
            return self.call if _id == self.call.id else None
        if model is McpArtifactRow:
            return self.artifact if _id == self.artifact.id else None
        return None


async def test_chat_artifact_requires_live_owner_apply_result() -> None:
    rows = Rows()
    principal = Principal(kind=PrincipalKind.USER, user_id=rows.owner_id)

    async def resolve() -> uuid.UUID:
        return await owned_chat_artifact_id(
            cast(AsyncSession, rows), principal, rows.conversation.id, rows.turn.id
        )

    assert await resolve() == rows.artifact_id
    with pytest.raises(ChatNotFound):
        await owned_chat_artifact_id(
            cast(AsyncSession, rows),
            Principal(kind=PrincipalKind.USER, user_id=uuid.uuid4()),
            rows.conversation.id,
            rows.turn.id,
        )
    with pytest.raises(ChatNotFound):
        await owned_chat_artifact_id(
            cast(AsyncSession, rows), principal, uuid.uuid4(), rows.turn.id
        )
    rows.conversation.deleted_at = datetime.now(UTC)
    with pytest.raises(ChatNotFound):
        await resolve()
    rows.conversation.deleted_at = None
    rows.call.run_id = uuid.uuid4()
    with pytest.raises(ChatNotFound):
        await resolve()
    rows.call.run_id = rows.run_id
    rows.artifact.call_id = uuid.uuid4()
    with pytest.raises(ChatNotFound):
        await resolve()
    rows.artifact.call_id = rows.call_id
    rows.call.state = McpCallState.FAILED
    with pytest.raises(ChatNotFound):
        await resolve()
    rows.call.state = McpCallState.SUCCEEDED
    rows.turn.action = "research"
    with pytest.raises(ChatNotFound):
        await resolve()


async def test_chat_artifact_route_delegates_digest_checked_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = Rows()
    principal = Principal(kind=PrincipalKind.USER, user_id=rows.owner_id)
    seen: list[uuid.UUID] = []

    async def download(artifact_id: uuid.UUID, *_args: object) -> object:
        seen.append(artifact_id)
        return object()

    monkeypatch.setattr(chat_routes, "download_artifact", download)
    await chat_routes.download_chat_artifact(
        rows.conversation.id,
        rows.turn.id,
        principal,
        cast(AsyncSession, rows),
        cast(Request, object()),
    )
    assert seen == [rows.artifact_id]


def test_chat_artifact_route_is_authenticated() -> None:
    app = create_app(Settings(_secrets_dir="/nonexistent"))  # type: ignore[call-arg]
    operation = app.openapi()["paths"][
        "/api/v1/chat/conversations/{conversation_id}/turns/{turn_id}/artifact"
    ]["get"]
    assert operation["security"] == [{"HTTPBearer": []}]
