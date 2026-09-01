"""Official SDK compatibility against the composed fake-engine topology."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import shutil
import socket
import struct
import subprocess
import time
from typing import Any
from urllib.parse import urlsplit

import anthropic
import httpx
import openai
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="set COIRE_INTEGRATION=1 to run gateway tests",
    ),
]

TEST_REPO = "mlx-community/Qwen2.5-0.5B-Instruct-4bit"


@pytest.fixture(scope="module")
def gateway_model(api_url: str, admin_headers: dict[str, str]) -> str:
    with httpx.Client(base_url=api_url, timeout=30) as client:
        models = client.get("/api/v1/admin/models", headers=admin_headers).json()
        found = next((model for model in models if model["repo_id"] == TEST_REPO), None)
        if found is None:
            response = client.post(
                "/api/v1/admin/models",
                headers=admin_headers,
                json={"repo_id": TEST_REPO, "tags": ["general"]},
            )
            assert response.status_code == 202, response.text
            found = response.json()
        deadline = time.monotonic() + 900
        while found["state"] not in ("ready", "failed") and time.monotonic() < deadline:
            time.sleep(2)
            found = client.get(f"/api/v1/admin/models/{found['id']}", headers=admin_headers).json()
        assert found["state"] == "ready", found.get("state_reason")
        response = client.patch(
            f"/api/v1/admin/models/{found['id']}",
            headers=admin_headers,
            json={"visibility": "published"},
        )
        assert response.status_code == 200, response.text
        return str(found["id"])


async def test_official_openai_sdk_lists_and_streams(
    api_url: str, gateway_model: str, admin_headers: dict[str, str]
) -> None:
    token = admin_headers["Authorization"].removeprefix("Bearer ")
    async with openai.AsyncOpenAI(api_key=token, base_url=f"{api_url}/v1") as client:
        listing = await client.models.list()
        assert gateway_model in {model.id for model in listing.data}
        stream = await client.chat.completions.create(
            model=gateway_model, messages=[{"role": "user", "content": "hello"}], stream=True
        )
        text = ""
        response_models: set[str] = set()
        async for chunk in stream:
            text += chunk.choices[0].delta.content or ""
            response_models.add(chunk.model)
    assert text.strip()
    assert response_models == {gateway_model}


async def test_official_anthropic_sdk_streams(
    api_url: str, gateway_model: str, admin_headers: dict[str, str]
) -> None:
    token = admin_headers["Authorization"].removeprefix("Bearer ")
    async with anthropic.AsyncAnthropic(api_key=token, base_url=api_url) as client:
        stream = await client.messages.create(
            model=gateway_model,
            max_tokens=8,
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
        )
        events: list[Any] = []
        async for event in stream:
            events.append(event)
    assert any(getattr(event, "type", None) == "message_stop" for event in events)


def test_unmodified_claude_code_cli_uses_gateway(
    api_url: str, gateway_model: str, admin_headers: dict[str, str]
) -> None:
    executable = shutil.which("claude")
    if executable is None:
        pytest.skip("Claude Code CLI is not installed on this validation host")
    environment = os.environ.copy()
    environment.update(
        {
            "ANTHROPIC_API_KEY": admin_headers["Authorization"].removeprefix("Bearer "),
            "ANTHROPIC_BASE_URL": api_url,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT": "1",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "128",
            "CLAUDE_CODE_MAX_RETRIES": "0",
            "CLAUDE_CODE_EXIT_AFTER_STOP_DELAY": "100",
            "MAX_THINKING_TOKENS": "0",
        }
    )
    result = subprocess.run(
        [
            executable,
            "--bare",
            "--print",
            "--settings",
            json.dumps(
                {"modelOverrides": {"claude-sonnet-4-6": gateway_model}}, separators=(",", ":")
            ),
            "--model",
            "claude-sonnet-4-6",
            "Reply with exactly: coire-cli-ok",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip(), "Claude Code completed without returning assistant text"


async def test_concurrent_cold_requests_share_the_load(
    api_url: str, gateway_model: str, admin_headers: dict[str, str]
) -> None:
    async with httpx.AsyncClient(base_url=api_url, timeout=30) as control:
        engines = (await control.get("/api/v1/admin/engines", headers=admin_headers)).json()
        for engine in engines:
            if engine.get("model_id") == gateway_model and engine.get("state") in (
                "ready",
                "starting",
            ):
                response = await control.delete(
                    f"/api/v1/admin/engines/{engine['id']}", headers=admin_headers
                )
                assert response.status_code in (200, 202, 204), response.text
    token = admin_headers["Authorization"].removeprefix("Bearer ")
    async with openai.AsyncOpenAI(api_key=token, base_url=f"{api_url}/v1") as client:
        results = await asyncio.gather(
            *(
                client.chat.completions.create(
                    model=gateway_model, messages=[{"role": "user", "content": f"hello {index}"}]
                )
                for index in range(3)
            )
        )
    assert all(result.choices[0].message.content for result in results)


async def test_cold_slow_stream_is_one_attributable_trace(
    api_url: str, gateway_model: str, admin_headers: dict[str, str]
) -> None:
    """FR-004/005: W3C context joins API/node spans and the induced decode delay dominates."""

    async with httpx.AsyncClient(base_url=api_url, timeout=30) as control:
        engines = (await control.get("/api/v1/admin/engines", headers=admin_headers)).json()
        for engine in engines:
            if engine.get("model_id") == gateway_model and engine.get("state") in {
                "ready",
                "starting",
            }:
                response = await control.delete(
                    f"/api/v1/admin/engines/{engine['id']}", headers=admin_headers
                )
                assert response.status_code in (200, 202, 204), response.text

    trace_id = secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    headers = {
        **admin_headers,
        "traceparent": f"00-{trace_id}-{span_id}-01",
    }
    async with (
        httpx.AsyncClient(base_url=api_url, timeout=60) as client,
        client.stream(
            "POST",
            "/v1/chat/completions",
            headers=headers,
            json={
                "model": gateway_model,
                "stream": True,
                "messages": [{"role": "user", "content": "slow-stream"}],
            },
        ) as response,
    ):
        assert response.status_code == 200, await response.aread()
        assert b"data: [DONE]" in await response.aread()

    encoded_trace_id = base64.b64encode(bytes.fromhex(trace_id)).decode()
    trace: dict[str, Any] | None = None
    required = {
        "coire.api.gateway",
        "coire.scheduler.admission_wait",
        "coire.node.load_wait",
        "coire.node.prefill",
        "coire.node.decode",
        "coire.node.network",
    }
    deadline = time.monotonic() + 30
    async with httpx.AsyncClient(
        base_url=api_url, auth=("admin", admin_headers["Authorization"].removeprefix("Bearer "))
    ) as client:
        while time.monotonic() < deadline:
            found = await client.get(
                f"/grafana/api/datasources/proxy/uid/tempo/api/traces/{trace_id}"
            )
            if found.status_code == 200:
                trace = found.json()
                names = {
                    span["name"]
                    for batch in trace["batches"]
                    for scope in batch["scopeSpans"]
                    for span in scope["spans"]
                }
                if required <= names:
                    break
            await asyncio.sleep(1)
    assert trace is not None, "trace did not reach Tempo"
    spans = [
        span
        for batch in trace["batches"]
        for scope in batch["scopeSpans"]
        for span in scope["spans"]
    ]
    assert {span["traceId"] for span in spans} == {encoded_trace_id}
    by_name = {span["name"]: span for span in spans}
    assert required <= by_name.keys()
    load_wait_s = (
        int(by_name["coire.node.load_wait"]["endTimeUnixNano"])
        - int(by_name["coire.node.load_wait"]["startTimeUnixNano"])
    ) / 1_000_000_000
    assert load_wait_s >= 0.5, f"cold-load delay was not attributed: {load_wait_s}"
    assert int(by_name["coire.node.decode"]["endTimeUnixNano"]) - int(
        by_name["coire.node.decode"]["startTimeUnixNano"]
    ) > int(by_name["coire.node.prefill"]["endTimeUnixNano"]) - int(
        by_name["coire.node.prefill"]["startTimeUnixNano"]
    )


@pytest.mark.parametrize(
    ("marker", "expected_span"),
    [
        ("slow-network", "coire.node.network"),
        ("slow-prefill", "coire.node.prefill"),
        ("slow-decode", "coire.node.decode"),
    ],
)
async def test_induced_node_stage_is_attributable(
    api_url: str,
    gateway_model: str,
    admin_headers: dict[str, str],
    marker: str,
    expected_span: str,
) -> None:
    """SC-001: controlled transport/model delays land in their distinct stage span."""
    trace_id = secrets.token_hex(16)
    headers = {
        **admin_headers,
        "traceparent": f"00-{trace_id}-{secrets.token_hex(8)}-01",
    }
    async with (
        httpx.AsyncClient(base_url=api_url, timeout=60) as client,
        client.stream(
            "POST",
            "/v1/chat/completions",
            headers=headers,
            json={
                "model": gateway_model,
                "stream": True,
                "messages": [{"role": "user", "content": marker}],
            },
        ) as response,
    ):
        assert response.status_code == 200, await response.aread()
        assert b"data: [DONE]" in await response.aread()

    encoded_trace_id = base64.b64encode(bytes.fromhex(trace_id)).decode()
    deadline = time.monotonic() + 30
    spans: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        base_url=api_url, auth=("admin", admin_headers["Authorization"].removeprefix("Bearer "))
    ) as client:
        while time.monotonic() < deadline:
            found = await client.get(
                f"/grafana/api/datasources/proxy/uid/tempo/api/traces/{trace_id}"
            )
            if found.status_code == 200:
                trace = found.json()
                spans = [
                    span
                    for batch in trace["batches"]
                    for scope in batch["scopeSpans"]
                    for span in scope["spans"]
                ]
                if any(span["name"] == expected_span for span in spans):
                    break
            await asyncio.sleep(1)
    matching = [span for span in spans if span["name"] == expected_span]
    assert matching and {span["traceId"] for span in matching} == {encoded_trace_id}
    duration_s = max(
        (int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"])) / 1_000_000_000
        for span in matching
    )
    assert duration_s >= 0.8, f"{expected_span} did not capture the induced delay: {duration_s}"


async def test_gateway_and_node_metrics_back_dashboard_queries(
    api_url: str, admin_headers: dict[str, str]
) -> None:
    """FR-006/008: dashboard metric names are emitted by live request/load paths."""
    required = {
        "coire_gateway_tokens_total",
        "coire_node_queue_depth",
        "coire_node_generation_output_bytes_total",
        "coire_engine_unload_seconds_count",
    }
    async with httpx.AsyncClient(base_url=api_url, timeout=30) as control:
        engines = (await control.get("/api/v1/admin/engines", headers=admin_headers)).json()
        ready = next(engine for engine in engines if engine.get("state") == "ready")
        stopped = await control.delete(
            f"/api/v1/admin/engines/{ready['id']}", headers=admin_headers
        )
        assert stopped.status_code in {200, 202, 204}, stopped.text
    token = admin_headers["Authorization"].removeprefix("Bearer ")
    deadline = time.monotonic() + 30
    observed: set[str] = set()
    async with httpx.AsyncClient(base_url=api_url, auth=("admin", token)) as client:
        while time.monotonic() < deadline:
            for metric in required - observed:
                response = await client.get(
                    "/grafana/api/datasources/proxy/uid/prometheus/api/v1/query",
                    params={"query": metric},
                )
                if response.status_code == 200 and response.json()["data"]["result"]:
                    observed.add(metric)
            if observed == required:
                break
            await asyncio.sleep(1)
    assert observed == required, f"dashboard metrics absent from Prometheus: {required - observed}"


def _usage_outcomes() -> list[str]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            "coire-it",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "coire",
            "-d",
            "coire",
            "-Atc",
            "select outcome::text from usage_records order by started_at",
        ],
        cwd="deploy/compose",
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


async def test_completed_and_refused_requests_are_persisted(
    api_url: str, gateway_model: str, admin_headers: dict[str, str]
) -> None:
    before = len(_usage_outcomes())
    async with httpx.AsyncClient(base_url=api_url) as client:
        completed = await client.post(
            "/v1/chat/completions",
            headers=admin_headers,
            json={"model": gateway_model, "messages": [{"role": "user", "content": "usage"}]},
        )
        refused = await client.post(
            "/v1/chat/completions",
            headers=admin_headers,
            json={
                "model": "00000000-0000-0000-0000-000000000000",
                "messages": [{"role": "user", "content": "usage"}],
            },
        )
    assert completed.status_code == 200
    assert refused.status_code == 404
    outcomes = _usage_outcomes()[before:]
    assert "succeeded" in outcomes
    assert "refused" in outcomes


async def test_failed_stream_is_persisted(
    api_url: str, gateway_model: str, admin_headers: dict[str, str]
) -> None:
    before = len(_usage_outcomes())
    async with (
        httpx.AsyncClient(base_url=api_url, timeout=10) as client,
        client.stream(
            "POST",
            "/v1/chat/completions",
            headers=admin_headers,
            json={
                "model": gateway_model,
                "stream": True,
                "messages": [{"role": "user", "content": "fail-stream"}],
            },
        ) as response,
    ):
        assert response.status_code == 200
        body = await response.aread()
        assert b"engine stream failed" in body
    deadline = time.monotonic() + 10
    outcomes: list[str] = []
    while time.monotonic() < deadline:
        outcomes = _usage_outcomes()[before:]
        if "failed" in outcomes:
            break
        await asyncio.sleep(0.2)
    assert "failed" in outcomes


async def test_abandoned_stream_is_persisted(
    direct_api_url: str, gateway_model: str, admin_headers: dict[str, str]
) -> None:
    before = len(_usage_outcomes())
    endpoint = urlsplit(direct_api_url)
    reader, writer = await asyncio.open_connection(endpoint.hostname, endpoint.port)
    body = json.dumps(
        {
            "model": gateway_model,
            "stream": True,
            "messages": [{"role": "user", "content": "slow-stream"}],
        }
    ).encode()
    writer.write(
        b"POST /v1/chat/completions HTTP/1.1\r\n"
        + f"Host: {endpoint.hostname}\r\n".encode()
        + b"Content-Type: application/json\r\n"
        + f"Authorization: {admin_headers['Authorization']}\r\n".encode()
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + body
    )
    await writer.drain()
    received = await asyncio.wait_for(reader.readuntil(b"data:"), timeout=10)
    assert b"HTTP/1.1 200" in received
    raw_socket = writer.get_extra_info("socket")
    raw_socket.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    writer.transport.abort()
    deadline = time.monotonic() + 10
    outcomes: list[str] = []
    while time.monotonic() < deadline:
        outcomes = _usage_outcomes()[before:]
        if "disconnected" in outcomes:
            break
        await asyncio.sleep(0.2)
    assert "disconnected" in outcomes
