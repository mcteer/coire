"""Composed MCP run cancellation, timeout, and service isolation checks."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from test_mcp_loop import SOURCE  # type: ignore[import-not-found]
from test_run_orchestration import prepare_verified_model  # type: ignore[import-not-found]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="set COIRE_INTEGRATION=1 for the disposable composed project",
    ),
]

COMPOSE_DIR = Path(__file__).resolve().parents[2] / "deploy" / "compose"
TERMINAL = {"succeeded", "failed", "result_collection_failed", "timed_out", "killed"}


@pytest.fixture(scope="module")
def mcp_runtime(api_url: str, admin_headers: dict[str, str]) -> Iterator[tuple[str, str, str]]:
    with httpx.Client(base_url=api_url, timeout=60) as client:
        model_id, variant_id = prepare_verified_model(client, admin_headers)
        model = client.get(f"/api/v1/admin/models/{model_id}", headers=admin_headers).json()
        prior_tags = model["tags"]
        changed = client.patch(
            f"/api/v1/admin/models/{model_id}",
            headers={**admin_headers, "If-Match": model["updated_at"]},
            json={"tags": sorted(set(prior_tags) | {"coding"})},
        )
        assert changed.status_code == 200, changed.text
        launched = client.post(
            "/api/v1/instances",
            headers=admin_headers,
            json={"model_id": model_id, "variant_id": variant_id, "policy": "single:coire-edge-a"},
        )
        assert launched.status_code == 202, launched.text
        instance_id = launched.json()["id"]
        _wait_instance(client, admin_headers, instance_id)
        created = client.post(
            "/api/v1/admin/users",
            headers=admin_headers,
            json={
                "email": f"mcp-life-{uuid.uuid4().hex[:12]}@integration.test",
                "display_name": "MCP lifecycle test",
                "role": "user",
            },
        )
        assert created.status_code == 201, created.text
        user_id = created.json()["id"]
        issued = client.post(
            f"/api/v1/admin/users/{user_id}/keys",
            headers=admin_headers,
            json={
                "name": "mcp-lifecycle",
                "scopes": ["mcp", "chat"],
                "requests_per_minute": 100,
                "monthly_budget_tokens": 100000,
            },
        )
        assert issued.status_code == 201, issued.text
        try:
            yield api_url, model_id, issued.json()["secret"]
        finally:
            client.delete(f"/api/v1/instances/{instance_id}", headers=admin_headers)
            current = client.get(f"/api/v1/admin/models/{model_id}", headers=admin_headers)
            if current.status_code == 200:
                client.patch(
                    f"/api/v1/admin/models/{model_id}",
                    headers={**admin_headers, "If-Match": current.json()["updated_at"]},
                    json={"tags": prior_tags},
                )
            client.delete(f"/api/v1/admin/users/{user_id}", headers=admin_headers)


def _wait_instance(client: httpx.Client, headers: dict[str, str], instance_id: str) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        result = client.get(f"/api/v1/instances/{instance_id}", headers=headers)
        assert result.status_code == 200, result.text
        if result.json()["state"] == "ready":
            return
        assert result.json()["state"] != "failed", result.text
        time.sleep(0.5)
    pytest.fail(f"MCP lifecycle instance {instance_id} did not become ready")


def _request(model_id: str, key: str, identifier: int) -> tuple[dict[str, str], dict[str, object]]:
    return (
        {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json, text/event-stream",
        },
        {
            "jsonrpc": "2.0",
            "id": identifier,
            "method": "tools/call",
            "params": {
                "name": "research",
                "arguments": {
                    "source": SOURCE,
                    "question": "mcp-lifecycle-slow: read README",
                    "model_id": model_id,
                },
            },
        },
    )


def _new_run(client: httpx.Client, headers: dict[str, str], before: set[str]) -> dict[str, Any]:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        response = client.get("/api/v1/admin/runs", headers=headers)
        assert response.status_code == 200, response.text
        for run in response.json():
            if run["id"] not in before and run["state"] == "running":
                return cast(dict[str, Any], run)
        time.sleep(0.1)
    pytest.fail("MCP call never reached a running Studio container")


def _run_ids(client: httpx.Client, headers: dict[str, str]) -> set[str]:
    response = client.get("/api/v1/admin/runs", headers=headers)
    assert response.status_code == 200, response.text
    return {row["id"] for row in response.json()}


def _terminal_run(
    client: httpx.Client, headers: dict[str, str], run_id: str, *, timeout: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get("/api/v1/admin/runs", headers=headers)
        assert response.status_code == 200, response.text
        run = next((item for item in response.json() if item["id"] == run_id), None)
        if run is not None and run["state"] in TERMINAL:
            return cast(dict[str, Any], run)
        time.sleep(0.1)
    pytest.fail(f"MCP run {run_id} did not terminate")


def _assert_stopped_and_revoked(run_id: str) -> None:
    containers = subprocess.check_output(
        ["docker", "ps", "--filter", f"label=com.coire.agent-run={run_id}", "--format", "{{.ID}}"],
        text=True,
    ).strip()
    assert not containers, f"run container still active: {containers}"
    state = subprocess.check_output(
        [
            "docker",
            "exec",
            "coire-it-postgres-1",
            "psql",
            "-U",
            "coire",
            "-d",
            "coire",
            "-t",
            "-A",
            "-c",
            f"SELECT revoked_at IS NOT NULL FROM run_tokens WHERE run_id='{run_id}'",
        ],
        text=True,
    ).strip()
    assert state == "t", f"run token was not revoked: {state!r}"


def test_admin_kill_terminates_mcp_run_within_five_seconds(
    mcp_runtime: tuple[str, str, str], admin_headers: dict[str, str]
) -> None:
    api_url, model_id, key = mcp_runtime
    with httpx.Client(base_url=api_url, timeout=60) as client:
        before = _run_ids(client, admin_headers)
        headers, body = _request(model_id, key, 201)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                lambda: httpx.post(f"{api_url}/mcp", headers=headers, json=body, timeout=90)
            )
            run = _new_run(client, admin_headers, before)
            started = time.monotonic()
            killed = client.request(
                "DELETE",
                f"/api/v1/admin/runs/{run['id']}",
                headers=admin_headers,
                json={"reason": "composed MCP lifecycle kill"},
            )
            assert killed.status_code == 202, killed.text
            terminal = _terminal_run(client, admin_headers, run["id"], timeout=5)
            assert terminal["state"] == "killed", terminal
            assert time.monotonic() - started < 5
            _assert_stopped_and_revoked(run["id"])
            assert future.result(timeout=30).json()["result"]["isError"] is True


def test_mcp_run_timeout_stops_container_and_revokes_token(
    mcp_runtime: tuple[str, str, str],
    admin_headers: dict[str, str],
) -> None:
    api_url, model_id, key = mcp_runtime
    with httpx.Client(base_url=api_url, timeout=75) as client:
        before = _run_ids(client, admin_headers)
        headers, body = _request(model_id, key, 202)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                lambda: httpx.post(f"{api_url}/mcp", headers=headers, json=body, timeout=75)
            )
            run = _new_run(client, admin_headers, before)
            terminal = _terminal_run(client, admin_headers, run["id"], timeout=45)
            assert terminal["state"] == "timed_out", terminal
            _assert_stopped_and_revoked(run["id"])
            assert future.result(timeout=20).json()["result"]["isError"] is True


@pytest.mark.asyncio
async def test_direct_mcp_disconnect_kills_run(
    mcp_runtime: tuple[str, str, str], direct_mcp_url: str, admin_headers: dict[str, str]
) -> None:
    api_url, model_id, key = mcp_runtime
    with httpx.Client(base_url=api_url, timeout=10) as monitor:
        before = _run_ids(monitor, admin_headers)
        headers, body = _request(model_id, key, 203)
        async with httpx.AsyncClient(base_url=direct_mcp_url, timeout=75) as client:
            call = asyncio.create_task(client.post("/mcp", headers=headers, json=body))
            run = await asyncio.to_thread(_new_run, monitor, admin_headers, before)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
        terminal = await asyncio.to_thread(
            _terminal_run, monitor, admin_headers, run["id"], timeout=5
        )
        assert terminal["state"] == "killed", terminal
        _assert_stopped_and_revoked(run["id"])


def test_mcp_only_restart_keeps_chat_available(mcp_runtime: tuple[str, str, str]) -> None:
    api_url, model_id, key = mcp_runtime
    headers = {"Authorization": f"Bearer {key}"}
    chat = {
        "model": model_id,
        "messages": [{"role": "user", "content": "hello"}],
    }
    with httpx.Client(base_url=api_url, timeout=15) as client:
        initial = client.post("/v1/chat/completions", headers=headers, json=chat)
        assert initial.status_code == 200, initial.text
        command = subprocess.Popen(
            ["docker", "compose", "-p", "coire-it", "restart", "coire-mcp"],
            cwd=COMPOSE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        samples = 0
        while command.poll() is None:
            response = client.post("/v1/chat/completions", headers=headers, json=chat)
            assert response.status_code == 200, response.text
            samples += 1
        stdout, stderr = command.communicate(timeout=10)
        assert command.returncode == 0, (stdout, stderr)
        assert samples > 0, "no chat traffic overlapped the MCP restart"
        final = client.post("/v1/chat/completions", headers=headers, json=chat)
        assert final.status_code == 200, final.text
