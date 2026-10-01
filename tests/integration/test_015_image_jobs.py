"""Deterministic private worker queue and replay acceptance without model weights."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from coire_core.models.image_worker import (
    ImageJobBinding,
    ImageWorkerLoadRequest,
    ImageWorkerRunRequest,
    ImageWorkerStatus,
)
from coire_core.models.images import ImageSpec, ResolvedImageSpec, canonical_spec_hash
from coire_node.image_runtime.control import create_worker_app
from coire_node.testing.fake_image_worker import FakeImagePipeline

pytestmark = pytest.mark.integration
TOKEN = "t" * 64
JOB = "01J00000000000000000000000"
SECOND = "01J00000000000000000000001"


def _requests() -> tuple[ImageWorkerLoadRequest, ImageWorkerRunRequest]:
    load = ImageWorkerLoadRequest(
        slug="studio--fake-image",
        model_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        manifest_sha256="a" * 64,
        reservation_bytes=1024,
        runtime_version="mflux-0.20.0",
    )
    spec = ImageSpec(
        model_id=load.model_id,
        prompt="queue fixture",
        width=64,
        height=64,
        steps=2,
        guidance=Decimal(0),
        seed=7,
    )
    resolved = ResolvedImageSpec(
        spec=spec,
        seeds=(7,),
        pipeline_version=load.runtime_version,
        environment_fingerprint="b" * 64,
        model_sha256=load.manifest_sha256,
        spec_hash=canonical_spec_hash(spec),
    )
    run = ImageWorkerRunRequest(
        job_id=JOB,
        attempt=1,
        fence=1,
        instance_id=load.instance_id,
        resolved=resolved,
        deadline_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    return load, run


async def _status(client: httpx.AsyncClient, command: ImageWorkerRunRequest) -> ImageWorkerStatus:
    response = await client.post(
        "/status",
        json=ImageJobBinding(
            job_id=command.job_id, attempt=command.attempt, fence=command.fence
        ).model_dump(mode="json"),
    )
    assert response.status_code == 200
    return ImageWorkerStatus.model_validate(response.json())


async def test_one_serial_worker_replays_exact_fence_without_second_generation(
    tmp_path: Path,
) -> None:
    load, first = _requests()
    pipeline = FakeImagePipeline(block_at_step=2)
    app = create_worker_app(load, pipeline, tmp_path / "scratch", token=TOKEN, port=39177)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://worker",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        assert (await client.put("/job", json=first.model_dump(mode="json"))).status_code == 202
        assert await asyncio.to_thread(pipeline.entered.wait, 2)
        second = first.model_copy(update={"job_id": SECOND})
        assert (await client.put("/job", json=second.model_dump(mode="json"))).status_code == 409
        assert (await client.put("/job", json=first.model_dump(mode="json"))).status_code == 200
        changed = first.model_copy(update={"fence": 2})
        assert (await client.put("/job", json=changed.model_dump(mode="json"))).status_code == 409
        pipeline.release.set()
        for _ in range(100):
            observed = await _status(client, first)
            if observed.state == "generated":
                break
            await asyncio.sleep(0.01)
        assert observed.state == "generated"
        assert len(observed.outputs) == 1
        assert pipeline.calls == 1
