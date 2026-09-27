"""Protocol enumeration and authentication for the independent MCP listener."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from pytest import MonkeyPatch

from coire_api import mcp_calls
from coire_api.auth import ANONYMOUS, Principal, PrincipalKind, bound_principal
from coire_api.db import McpCallRow
from coire_core.models.mcp import (
    McpToolName,
    PlanInput,
    ResearchInput,
    ResearchResult,
    WorkspaceSource,
)
from coire_mcp import main
from coire_mcp import tools as mcp_tools


def _principal(*, scoped: bool = True) -> Principal:
    return Principal(
        kind=PrincipalKind.API_KEY,
        user_id=uuid.uuid4(),
        scopes=frozenset({"mcp"}) if scoped else frozenset({"read"}),
    )


async def _request(
    monkeypatch: MonkeyPatch, principal: Principal, *, origin: str | None = None
) -> httpx.Response:
    async def authenticate(_request: object) -> Principal:
        return principal

    monkeypatch.setattr(main, "authenticate_request", authenticate)
    app = main.create_app()
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    if origin is not None:
        headers["origin"] = origin
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8001"
        ) as client,
    ):
        return await client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )


async def test_mcp_lists_exactly_three_tools(monkeypatch: MonkeyPatch) -> None:
    response = await _request(monkeypatch, _principal())
    assert response.status_code == 200, response.text
    assert {tool["name"] for tool in response.json()["result"]["tools"]} == {
        "research",
        "plan",
        "apply",
    }


async def test_mcp_refuses_anonymous_and_unscoped_credentials(monkeypatch: MonkeyPatch) -> None:
    assert (await _request(monkeypatch, ANONYMOUS)).status_code == 401
    assert (await _request(monkeypatch, _principal(scoped=False))).status_code == 403


async def test_mcp_rejects_browser_origin(monkeypatch: MonkeyPatch) -> None:
    response = await _request(monkeypatch, _principal(), origin="https://attacker.example")
    assert response.status_code == 403


async def test_mcp_rejects_oversized_body_and_extra_paths(monkeypatch: MonkeyPatch) -> None:
    async def authenticate(_request: object) -> Principal:
        return _principal()

    monkeypatch.setattr(main, "authenticate_request", authenticate)
    app = main.create_app()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8001"
        ) as client,
    ):
        oversized = await client.post("/mcp", content=b"{}", headers={"content-length": "4194305"})
        extra = await client.post("/mcp/admin", json={})
    assert oversized.status_code == 413
    assert extra.status_code == 404


async def test_research_invocation_uses_strict_top_level_input(monkeypatch: MonkeyPatch) -> None:
    async def authenticate(_request: object) -> Principal:
        return _principal()

    async def execute(tool: McpToolName, payload: ResearchInput, task: str) -> dict[str, object]:
        assert tool is McpToolName.RESEARCH
        assert payload.source.revision == "main"
        assert task == "Where is main?"
        assert bound_principal().kind is PrincipalKind.API_KEY
        return {
            "result_id": str(uuid.uuid4()),
            "run_id": str(uuid.uuid4()),
            "source_revision": "a" * 40,
            "answer": "main.py:1",
            "citations": [{"path": "main.py", "line": 1, "excerpt": "main"}],
        }

    monkeypatch.setattr(main, "authenticate_request", authenticate)
    monkeypatch.setattr(mcp_tools, "_execute", execute)
    app = main.create_app()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8001"
        ) as client,
    ):
        response = await client.post(
            "/mcp",
            headers={"accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "research",
                    "arguments": {
                        "source": {
                            "repository_url": "https://github.com/org/repo.git",
                            "revision": "main",
                        },
                        "question": "Where is main?",
                    },
                },
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["result"]["structuredContent"]["answer"] == "main.py:1"


async def test_transport_cancellation_reaches_tool(monkeypatch: MonkeyPatch) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def authenticate(_request: object) -> Principal:
        return _principal()

    async def execute(_tool: McpToolName, _payload: ResearchInput, _task: str) -> dict[str, object]:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {}

    monkeypatch.setattr(main, "authenticate_request", authenticate)
    monkeypatch.setattr(mcp_tools, "_execute", execute)
    app = main.create_app()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8001"
        ) as client,
    ):
        pending = asyncio.create_task(
            client.post(
                "/mcp",
                headers={"accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "research",
                        "arguments": {
                            "source": {
                                "repository_url": "https://github.com/org/repo.git",
                                "revision": "main",
                            },
                            "question": "Wait",
                        },
                    },
                },
            )
        )
        await asyncio.wait_for(started.wait(), 3)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await asyncio.wait_for(cancelled.wait(), 3)


async def test_plan_pins_prior_research_revision(monkeypatch: MonkeyPatch) -> None:
    prior_id = uuid.uuid4()
    source = WorkspaceSource.model_validate(
        {"repository_url": "https://github.com/org/repo.git", "revision": "main"}
    )
    prior = ResearchResult.model_validate(
        {
            "result_id": str(prior_id),
            "run_id": str(uuid.uuid4()),
            "source_revision": "a" * 40,
            "answer": "Found main",
            "citations": [{"path": "main.py", "line": 1}],
        }
    )

    async def authenticate(_request: object) -> Principal:
        return _principal()

    @asynccontextmanager
    async def session_scope() -> AsyncIterator[object]:
        yield object()

    async def get_call(*_args: object, **_kwargs: object) -> McpCallRow:
        return McpCallRow(source=source.model_dump(mode="json"))

    async def get_result(*_args: object, **_kwargs: object) -> ResearchResult:
        return prior

    async def execute(tool: McpToolName, payload: PlanInput, _task: str) -> dict[str, object]:
        assert tool is McpToolName.PLAN
        assert payload.source.revision == "a" * 40
        return {
            "result_id": str(uuid.uuid4()),
            "run_id": str(uuid.uuid4()),
            "source_revision": "a" * 40,
            "goal": "Change value",
            "steps": [{"description": "Update main.py", "acceptance_criteria": ["VALUE = 2"]}],
        }

    monkeypatch.setattr(main, "authenticate_request", authenticate)
    monkeypatch.setattr(mcp_tools, "session_scope", session_scope)
    monkeypatch.setattr(mcp_calls, "get_owned_call", get_call)
    monkeypatch.setattr(mcp_calls, "get_owned_result", get_result)
    monkeypatch.setattr(mcp_tools, "_execute", execute)
    app = main.create_app()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8001"
        ) as client,
    ):
        response = await client.post(
            "/mcp",
            headers={"accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "plan",
                    "arguments": {
                        "source": source.model_dump(mode="json"),
                        "goal": "Change value",
                        "research_result_id": str(prior_id),
                    },
                },
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["result"]["structuredContent"]["source_revision"] == "a" * 40
