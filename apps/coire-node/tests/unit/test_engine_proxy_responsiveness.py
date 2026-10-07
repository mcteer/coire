"""A native ownership lock cannot occupy the ASGI request event loop."""

import asyncio
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import psutil
import pytest
from fastapi import Request

from coire_core.models.engine import EngineState
from coire_core.models.gateway import ChatMessage, EngineChatRequest
from coire_node.routes import engines as routes


async def test_proxy_ownership_wait_leaves_request_loop_responsive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    identity = uuid.uuid4()

    class BusyEngines:
        def get(self, engine_id: uuid.UUID) -> SimpleNamespace:
            assert engine_id == identity
            entered.set()
            release.wait(timeout=1)
            finished.set()
            return SimpleNamespace(state=EngineState.READY, slug="fixture", port=9500, target=None)

    class Upstream:
        async def post(self, *args: object, **kwargs: object) -> httpx.Response:
            return httpx.Response(
                200, json={"ok": True}, request=httpx.Request("POST", "http://test")
            )

    monkeypatch.setattr(routes, "_engine_client", lambda: Upstream())
    task = asyncio.create_task(
        routes.proxy_chat_completion(
            identity,
            EngineChatRequest(model="fixture", messages=[ChatMessage(role="user", content="hi")]),
            BusyEngines(),  # type: ignore[arg-type]
            SimpleNamespace(path_for=lambda _: Path("/models/fixture")),  # type: ignore[arg-type]
            Request({"type": "http", "headers": []}),
        )
    )
    try:
        await asyncio.wait_for(asyncio.to_thread(entered.wait), timeout=2)
        await asyncio.sleep(0)
        assert not finished.is_set(), "Ownership lookup blocked the request event loop"
    finally:
        release.set()
        response = await task
    assert response.status_code == 200


async def test_reconciliation_inventory_does_not_occupy_request_loop_or_ownership_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_core.models.engine import ReconcileRequest
    from coire_node.testing.harness import Agent

    agent = Agent(tmp_path)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def inventory(*args: object, **kwargs: object) -> list[object]:
        entered.set()
        release.wait(timeout=1)
        finished.set()
        return []

    monkeypatch.setattr(psutil, "process_iter", inventory)
    task = asyncio.create_task(routes.reconcile(ReconcileRequest(expected=[]), agent.engines))
    try:
        await asyncio.wait_for(asyncio.to_thread(entered.wait), timeout=2)
        await asyncio.sleep(0)
        assert not finished.is_set(), "Process inventory occupied the request event loop"
        snapshot = await asyncio.wait_for(
            asyncio.to_thread(agent.engines.get, uuid.uuid4()), timeout=0.2
        )
        assert snapshot is None and not finished.is_set(), "Inventory held the ownership lock"
    finally:
        release.set()
        await task
