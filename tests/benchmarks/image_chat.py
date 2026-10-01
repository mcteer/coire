"""Repeatable same-node chat/image probe; writes only bounded measurement facts.

Run manually against an isolated development stack with an image model pinned to
``--node``. This driver never acquires a model or changes placement policy.
The report is evidence for review, not an automatic coexistence approval.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import statistics
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field


class ChatSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_token_ms: float = Field(ge=0)
    decode_tokens_per_second: float = Field(ge=0)
    completion_tokens: int = Field(ge=1)


class ImageSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    elapsed_seconds: float = Field(ge=0)
    progress_steps: int = Field(ge=0)
    succeeded: bool
    same_node: bool


class NodeSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thermal_state: str = Field(max_length=32)
    memory_used_bytes: int = Field(ge=0)
    memory_committed_bytes: int = Field(ge=0)


class MixedReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    node: str
    started_at: datetime
    elapsed_seconds: float = Field(ge=0)
    chat_samples: int = Field(ge=0)
    first_token_p50_ms: float | None = None
    first_token_p95_ms: float | None = None
    decode_tokens_per_second_p50: float | None = None
    gateway_overhead_p95_ms: float | None = None
    image_jobs: int = Field(ge=0)
    image_succeeded: int = Field(ge=0)
    image_progress_steps: int = Field(ge=0)
    images_on_requested_node: bool
    thermal_states: list[str]
    peak_node_memory_used_bytes: int | None = None
    peak_node_committed_bytes: int | None = None
    missing_evidence: list[str]


def _percentile(values: list[float], percentage: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(percentage * len(ordered)) - 1
    return ordered[max(0, rank)]


def summarize(
    *,
    node: str,
    started_at: datetime,
    elapsed_seconds: float,
    chats: list[ChatSample],
    images: list[ImageSample],
    nodes: list[NodeSample],
    gateway_overhead_p95_ms: float | None,
) -> MixedReport:
    missing: list[str] = []
    if elapsed_seconds < 900:
        missing.append("15_minute_duration")
    if not chats:
        missing.append("chat_samples")
    if not images or not any(item.progress_steps > 0 for item in images):
        missing.append("image_progress")
    if not nodes:
        missing.append("node_thermal_and_footprint")
    if gateway_overhead_p95_ms is None:
        missing.append("gateway_overhead")
    if images and not all(item.same_node for item in images):
        missing.append("same_node_image_placement")
    return MixedReport(
        node=node,
        started_at=started_at,
        elapsed_seconds=elapsed_seconds,
        chat_samples=len(chats),
        first_token_p50_ms=_percentile([item.first_token_ms for item in chats], 0.50),
        first_token_p95_ms=_percentile([item.first_token_ms for item in chats], 0.95),
        decode_tokens_per_second_p50=(
            statistics.median(item.decode_tokens_per_second for item in chats) if chats else None
        ),
        gateway_overhead_p95_ms=gateway_overhead_p95_ms,
        image_jobs=len(images),
        image_succeeded=sum(item.succeeded for item in images),
        image_progress_steps=sum(item.progress_steps for item in images),
        images_on_requested_node=bool(images) and all(item.same_node for item in images),
        thermal_states=sorted({item.thermal_state for item in nodes}),
        peak_node_memory_used_bytes=max((item.memory_used_bytes for item in nodes), default=None),
        peak_node_committed_bytes=max(
            (item.memory_committed_bytes for item in nodes), default=None
        ),
        missing_evidence=missing,
    )


async def _chat(client: httpx.AsyncClient, model: str, node: str) -> ChatSample:
    start = time.monotonic()
    first: float | None = None
    last: float | None = None
    tokens: int | None = None
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": "Describe a blue square in one sentence."}],
            "max_tokens": 64,
            "stream": True,
            "coire_affinity_node": node,
        },
        timeout=120.0,
    ) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            event = json.loads(line[6:])
            if not isinstance(event, dict):
                raise ValueError("invalid chat stream event")
            choices = event.get("choices")
            if isinstance(choices, list) and choices:
                delta = choices[0].get("delta")
                if isinstance(delta, dict) and delta.get("content"):
                    first = first or time.monotonic()
                    last = time.monotonic()
            usage = event.get("usage")
            if isinstance(usage, dict) and isinstance(usage.get("completion_tokens"), int):
                tokens = usage["completion_tokens"]
    if first is None or last is None or tokens is None or tokens < 1:
        raise ValueError("chat stream lacks first token or usage evidence")
    return ChatSample(
        first_token_ms=(first - start) * 1000,
        decode_tokens_per_second=(tokens - 1) / max(last - first, 0.001),
        completion_tokens=tokens,
    )


async def _image(client: httpx.AsyncClient, model: str, node: str) -> ImageSample:
    start = time.monotonic()
    response = await client.post(
        "/api/v1/images",
        json={
            "schema_version": 1,
            "model_id": model,
            "prompt": "A blue square on a white background.",
            "width": 512,
            "height": 512,
            "steps": 4,
            "guidance": 0,
            "n": 1,
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    response.raise_for_status()
    job_id = response.json()["job_id"]
    progress = 0
    while time.monotonic() - start < 300:
        observed = await client.get(f"/api/v1/images/{job_id}")
        observed.raise_for_status()
        job = observed.json()
        progress = max(progress, int(job.get("progress_step") or 0))
        if job["state"] in {"succeeded", "failed", "cancelled"}:
            resolved = job.get("resolved")
            same_node = False
            if isinstance(resolved, dict):
                expected = hashlib.sha256(
                    f"{resolved['pipeline_version']}\n{resolved['model_sha256']}\n{node}".encode()
                ).hexdigest()
                same_node = resolved.get("environment_fingerprint") == expected
            return ImageSample(
                elapsed_seconds=time.monotonic() - start,
                progress_steps=progress,
                succeeded=job["state"] == "succeeded",
                same_node=same_node,
            )
        await asyncio.sleep(1)
    raise TimeoutError("image job did not reach a terminal state in five minutes")


async def _node(client: httpx.AsyncClient, name: str) -> NodeSample:
    response = await client.get("/api/v1/admin/nodes")
    response.raise_for_status()
    nodes = response.json()
    node = next((item for item in nodes if item.get("name") == name), None)
    status = node.get("status") if isinstance(node, dict) else None
    if not isinstance(status, dict):
        raise ValueError("requested node has no fresh status")
    return NodeSample(
        thermal_state=str(status["thermal_state"]),
        memory_used_bytes=int(status["memory_total_bytes"]) - int(status["memory_free_bytes"]),
        memory_committed_bytes=int(status["memory_committed_bytes"]),
    )


async def _gateway_overhead(client: httpx.AsyncClient) -> float | None:
    query = "histogram_quantile(0.95, sum by (le) (rate(coire_gateway_overhead_duration_ms_bucket[5m])))"
    response = await client.get("/api/v1/query", params={"query": query})
    response.raise_for_status()
    rows = response.json().get("data", {}).get("result", [])
    if len(rows) != 1:
        return None
    value = float(rows[0]["value"][1])
    return value if math.isfinite(value) and value >= 0 else None


async def run(args: argparse.Namespace) -> MixedReport:
    token = (await asyncio.to_thread(Path(args.bearer_file).read_text)).strip()
    admin_token = (await asyncio.to_thread(Path(args.admin_bearer_file).read_text)).strip()
    if not token or not admin_token:
        raise ValueError("benchmark credential file is empty")
    started_at = datetime.now(UTC)
    started = time.monotonic()
    chats: list[ChatSample] = []
    images: list[ImageSample] = []
    nodes: list[NodeSample] = []
    async with (
        httpx.AsyncClient(
            base_url=args.url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
            trust_env=False,
        ) as client,
        httpx.AsyncClient(
            base_url=args.url,
            headers={"Authorization": f"Bearer {admin_token}"},
            timeout=30.0,
            trust_env=False,
        ) as admin,
    ):

        async def chat_loop() -> None:
            while time.monotonic() - started < args.duration_seconds:
                chats.append(await _chat(client, str(args.chat_model), args.node))

        async def image_loop() -> None:
            while time.monotonic() - started < args.duration_seconds:
                images.append(await _image(client, str(args.image_model), args.node))

        async def node_loop() -> None:
            while time.monotonic() - started < args.duration_seconds:
                nodes.append(await _node(admin, args.node))
                await asyncio.sleep(5)

        async with asyncio.TaskGroup() as group:
            group.create_task(chat_loop())
            group.create_task(image_loop())
            group.create_task(node_loop())
        async with httpx.AsyncClient(base_url=args.prometheus_url, trust_env=False) as prometheus:
            overhead = await _gateway_overhead(prometheus)
    return summarize(
        node=args.node,
        started_at=started_at,
        elapsed_seconds=time.monotonic() - started,
        chats=chats,
        images=images,
        nodes=nodes,
        gateway_overhead_p95_ms=overhead,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--prometheus-url", required=True)
    parser.add_argument("--bearer-file", required=True)
    parser.add_argument("--admin-bearer-file", required=True)
    parser.add_argument("--chat-model", type=uuid.UUID, required=True)
    parser.add_argument("--image-model", type=uuid.UUID, required=True)
    parser.add_argument("--node", choices=("coire-edge-a", "coire-edge-b"), required=True)
    parser.add_argument("--duration-seconds", type=int, default=900)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.duration_seconds < 1:
        parser.error("duration must be positive")
    report = asyncio.run(run(args))
    args.report.write_text(report.model_dump_json(indent=2) + "\n")
    print(f"Benchmark report written to {args.report}")


if __name__ == "__main__":
    main()
