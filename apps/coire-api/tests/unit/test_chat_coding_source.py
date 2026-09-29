"""Browser coding requests pin owned workspace and prior result revisions."""

from __future__ import annotations

import uuid
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.coding_calls import prepare_chat_coding_request
from coire_api.db import McpCallRow, RegisteredWorkspaceRow
from coire_core.errors import ChatConflict, ChatNotFound
from coire_core.models.chat import ChatTurnCreate
from coire_core.models.mcp import (
    McpCallState,
    McpToolName,
    PlanResult,
    ResearchResult,
    WorkspaceSource,
)


class Rows:
    def __init__(self) -> None:
        self.owner_id = uuid.uuid4()
        self.workspace_id = uuid.uuid4()
        self.research_id = uuid.uuid4()
        self.plan_id = uuid.uuid4()
        self.research_run = uuid.uuid4()
        self.plan_run = uuid.uuid4()
        self.workspace = RegisteredWorkspaceRow(
            id=self.workspace_id,
            owner_user_id=self.owner_id,
            repository_url="https://github.com/org/repo.git",
        )
        source = WorkspaceSource(workspace_id=self.workspace_id, revision="HEAD")
        self.research = McpCallRow(
            id=self.research_id,
            owner_user_id=self.owner_id,
            tool=McpToolName.RESEARCH,
            state=McpCallState.SUCCEEDED,
            run_id=self.research_run,
            source=source.model_dump(mode="json"),
            result=ResearchResult(
                result_id=uuid.uuid4(),
                run_id=self.research_run,
                source_revision="a" * 40,
                answer="Found code",
                citations=[{"path": "main.py", "line": 1}],  # type: ignore[list-item]
            ).model_dump(mode="json"),
        )
        self.plan = McpCallRow(
            id=self.plan_id,
            owner_user_id=self.owner_id,
            tool=McpToolName.PLAN,
            state=McpCallState.SUCCEEDED,
            run_id=self.plan_run,
            source=source.model_dump(mode="json"),
            result=PlanResult(
                result_id=uuid.uuid4(),
                run_id=self.plan_run,
                source_revision="a" * 40,
                goal="Change code",
                steps=[{"description": "edit", "acceptance_criteria": ["test"]}],  # type: ignore[list-item]
            ).model_dump(mode="json"),
        )

    async def scalar(self, statement: object) -> RegisteredWorkspaceRow | None:
        assert "registered_workspaces.owner_user_id" in str(statement)
        return self.workspace if self.workspace.owner_user_id == self.owner_id else None

    async def get(self, model: type, identifier: uuid.UUID) -> McpCallRow | None:
        if model is McpCallRow:
            return next((row for row in (self.research, self.plan) if row.id == identifier), None)
        return None


def _body(rows: Rows, action: str, **changes: object) -> ChatTurnCreate:
    return ChatTurnCreate.model_validate(
        {
            "client_request_id": str(uuid.uuid4()),
            "expected_revision": 1,
            "model_id": str(uuid.uuid4()),
            "content": "Change code",
            "workspace_id": str(rows.workspace_id),
            "action": action,
            **changes,
        }
    )


async def test_chat_code_source_and_prior_result_binding() -> None:
    rows = Rows()
    principal = Principal(kind=PrincipalKind.USER, user_id=rows.owner_id)

    async def prepare(body: ChatTurnCreate):  # type: ignore[no-untyped-def]
        return await prepare_chat_coding_request(cast(AsyncSession, rows), principal, body)

    research = await prepare(_body(rows, "research"))
    assert research.tool is McpToolName.RESEARCH
    assert research.input.source.revision == "HEAD"
    plan = await prepare(_body(rows, "plan", research_id=str(rows.research_id)))
    assert plan.tool is McpToolName.PLAN
    assert plan.input.source.revision == "a" * 40
    assert "Prior research" in plan.task
    apply = await prepare(_body(rows, "apply", plan_id=str(rows.plan_id)))
    assert apply.tool is McpToolName.APPLY
    assert apply.input.source.revision == "a" * 40
    assert "Change code" in apply.task

    with pytest.raises(ChatConflict, match="plan"):
        await prepare(_body(rows, "apply"))
    with pytest.raises(ChatConflict, match="revision"):
        await prepare(_body(rows, "apply", plan_id=str(rows.plan_id), source_revision="b" * 40))
    rows.plan.owner_user_id = uuid.uuid4()
    with pytest.raises(ChatNotFound):
        await prepare(_body(rows, "apply", plan_id=str(rows.plan_id)))
    rows.plan.owner_user_id = rows.owner_id
    rows.plan.source = WorkspaceSource(workspace_id=uuid.uuid4(), revision="HEAD").model_dump(
        mode="json"
    )
    with pytest.raises(ChatConflict, match="workspace"):
        await prepare(_body(rows, "apply", plan_id=str(rows.plan_id)))


async def test_chat_code_workspace_must_be_owned() -> None:
    rows = Rows()
    rows.workspace.owner_user_id = uuid.uuid4()
    with pytest.raises(ChatNotFound):
        await prepare_chat_coding_request(
            cast(AsyncSession, rows),
            Principal(kind=PrincipalKind.USER, user_id=rows.owner_id),
            _body(rows, "research"),
        )
