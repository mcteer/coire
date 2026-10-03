"""The benchmark report cannot claim evidence from missing or wrong-node samples."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from .image_chat import (
    ChatSample,
    ImageSample,
    NodeSample,
    _chat,
    _gateway_overhead,
    _image,
    _node,
    _node_identity,
    image_environment_fingerprint,
    summarize,
)


def test_report_percentiles_and_missing_evidence_are_explicit() -> None:
    started = datetime.now(UTC)
    report = summarize(
        node="coire-edge-b",
        started_at=started,
        elapsed_seconds=900,
        chats=[
            ChatSample(
                first_token_ms=100 + index * 10,
                decode_tokens_per_second=20,
                completion_tokens=4,
                prompt_tokens=3929,
            )
            for index in range(20)
        ],
        images=[ImageSample(elapsed_seconds=3, progress_steps=4, succeeded=True, same_node=True)],
        nodes=[
            NodeSample(thermal_state="normal", memory_used_bytes=100, memory_committed_bytes=80)
        ],
        gateway_overhead_p95_ms=12,
    )
    assert report.first_token_p50_ms == 190
    assert report.first_token_p95_ms == 280
    assert report.prompt_tokens_min == report.prompt_tokens_max == 3929
    assert report.images_on_requested_node
    assert report.peak_node_memory_used_bytes == 100
    assert report.missing_evidence == []

    incomplete = summarize(
        node="coire-edge-b",
        started_at=started,
        elapsed_seconds=1,
        chats=[],
        images=[ImageSample(elapsed_seconds=1, progress_steps=0, succeeded=False, same_node=False)],
        nodes=[],
        gateway_overhead_p95_ms=None,
    )
    assert {
        "15_minute_duration",
        "chat_samples",
        "image_progress",
        "node_thermal_and_footprint",
        "gateway_overhead",
        "same_node_image_placement",
    } == set(incomplete.missing_evidence)

    short = summarize(
        node="coire-edge-b",
        started_at=started,
        elapsed_seconds=900,
        chats=[
            ChatSample(
                first_token_ms=100,
                decode_tokens_per_second=20,
                completion_tokens=4,
                prompt_tokens=24,
            )
        ],
        images=[ImageSample(elapsed_seconds=3, progress_steps=4, succeeded=True, same_node=True)],
        nodes=[
            NodeSample(thermal_state="normal", memory_used_bytes=100, memory_committed_bytes=80)
        ],
        gateway_overhead_p95_ms=12,
    )
    assert short.missing_evidence == ["full_length_chat_prompts"]


def test_environment_fingerprint_distinguishes_identical_studios() -> None:
    common = {
        "memory_total_bytes": 1000,
        "gpu_cores": 16,
        "agent_version": "0.2.0",
        "model_sha256": "a" * 64,
    }
    assert image_environment_fingerprint(node="coire-edge-a", **common) != (
        image_environment_fingerprint(node="coire-edge-b", **common)
    )


async def test_probes_collect_stream_usage_fenced_progress_and_node_footprint() -> None:
    node = "coire-edge-b"
    model_sha = "a" * 64
    fingerprint = image_environment_fingerprint(
        node=node,
        memory_total_bytes=1000,
        gpu_cores=16,
        agent_version="0.2.0",
        model_sha256=model_sha,
    )
    polls = 0

    def responder(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        if request.url.path == "/v1/chat/completions":
            assert request.headers["Authorization"] == "Bearer user-key"
            assert request.read().decode().find('"coire_affinity_node":"coire-edge-b"') >= 0
            assert b'"stream_options":{"include_usage":true}' in request.content
            assert request.content.count(b"blue ") == 3900
            return httpx.Response(
                200,
                text=(
                    'data: {"choices":[{"delta":{"content":"blue"}}]}\n\n'
                    'data: {"usage":{"completion_tokens":4,"prompt_tokens":3929}}\n\n'
                    "data: [DONE]\n\n"
                ),
                headers={"content-type": "text/event-stream"},
            )
        if request.url.path == "/api/v1/images" and request.method == "POST":
            assert request.headers["Idempotency-Key"]
            return httpx.Response(202, json={"job_id": "01J00000000000000000000000"})
        if request.url.path.startswith("/api/v1/images/"):
            polls += 1
            return httpx.Response(
                200,
                json={
                    "state": "running" if polls == 1 else "succeeded",
                    "progress_step": polls,
                    "resolved": {
                        "pipeline_version": "mflux-0.20.0",
                        "model_sha256": model_sha,
                        "environment_fingerprint": fingerprint,
                    },
                },
            )
        if request.url.path == "/api/v1/admin/nodes":
            return httpx.Response(
                200,
                json=[
                    {
                        "name": node,
                        "status": {
                            "thermal_state": "normal",
                            "memory_total_bytes": 1000,
                            "memory_free_bytes": 400,
                            "memory_committed_bytes": 500,
                            "agent_version": "0.2.0",
                        },
                    }
                ],
            )
        if request.url.path == "/api/v1/query":
            assert "coire_gateway_overhead_duration_ms_milliseconds_bucket" in str(request.url)
            assert 'node="coire-edge-b"' in request.url.params["query"]
            assert 'protocol="openai"' in request.url.params["query"]
            return httpx.Response(200, json={"data": {"result": [{"value": [0, "12.5"]}]}})
        raise AssertionError(f"unexpected path {request.url.path}")

    async with httpx.AsyncClient(
        base_url="http://bench.test",
        headers={"Authorization": "Bearer user-key"},
        transport=httpx.MockTransport(responder),
    ) as client:
        chat = await _chat(client, "00000000-0000-0000-0000-000000000001", node)
        image = await _image(
            client,
            "00000000-0000-0000-0000-000000000002",
            node,
            1000,
            16,
            "0.2.0",
        )
        sampled_node = await _node(client, node)
        assert await _node_identity(client, node) == (1000, "0.2.0")
        overhead = await _gateway_overhead(client, node)
    assert chat.completion_tokens == 4 and chat.first_token_ms >= 0
    assert chat.prompt_tokens == 3929
    assert image.succeeded and image.same_node and image.progress_steps == 2
    assert sampled_node.memory_used_bytes == 600
    assert overhead == 12.5
