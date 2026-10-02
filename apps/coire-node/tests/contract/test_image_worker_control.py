"""The loopback worker accepts only fenced, authenticated typed commands."""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from coire_core.models.image_worker import (
    ImageJobBinding,
    ImageWorkerCancelRequest,
    ImageWorkerLoadRequest,
    ImageWorkerOutputManifest,
    ImageWorkerRunRequest,
    ImageWorkerStatus,
)
from coire_core.models.images import (
    ImageClassificationResult,
    ImageContentTag,
    ImageSpec,
    ResolvedImageSpec,
    canonical_spec_hash,
)
from coire_node.image_runtime.classification import CLASSIFIER_REVISION
from coire_node.image_runtime.control import create_worker_app
from coire_node.image_worker import Progress

TOKEN = "t" * 64
JOB = "01J00000000000000000000000"


class FakePipeline:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.calls = 0

    def generate(self, resolved: ResolvedImageSpec, callback: Progress) -> tuple[Image.Image, ...]:
        self.calls += 1
        callback(0, 1, 2)
        self.release.wait(timeout=2)
        callback(0, 2, 2)
        return (Image.new("RGB", (64, 64)),)


def _requests() -> tuple[ImageWorkerLoadRequest, ImageWorkerRunRequest]:
    load = ImageWorkerLoadRequest(
        slug="studio--z-image-turbo",
        model_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        manifest_sha256="a" * 64,
        reservation_bytes=1024,
        runtime_version="mflux-0.20.0",
    )
    spec = ImageSpec(
        model_id=load.model_id,
        prompt="private",
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
    return load, ImageWorkerRunRequest(
        job_id=JOB,
        attempt=1,
        fence=2,
        instance_id=load.instance_id,
        resolved=resolved,
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
    )


async def _wait_status(
    client: httpx.AsyncClient, binding: ImageJobBinding, state: str
) -> ImageWorkerStatus:
    for _ in range(100):
        response = await client.post("/status", json=binding.model_dump(mode="json"))
        assert response.status_code == 200
        status = ImageWorkerStatus.model_validate(response.json())
        if status.state == state:
            return status
        await asyncio.sleep(0.01)
    pytest.fail(f"worker never reached {state}")


def test_status_manifest_is_strict_and_has_no_path() -> None:
    output = ImageWorkerOutputManifest(
        index=0, byte_count=100, sha256="a" * 64, recipe_sha256="b" * 64
    )
    assert output.index == 0
    with pytest.raises(ValidationError):
        ImageWorkerOutputManifest.model_validate({**output.model_dump(), "path": "/private"})
    with pytest.raises(ValidationError):
        ImageWorkerStatus(
            job_id=JOB,
            attempt=1,
            fence=2,
            state="generated",
            updated_at=datetime.now(UTC),
        )


async def test_control_auth_replay_and_generated_manifest(tmp_path: Path) -> None:
    load, run = _requests()
    pipeline = FakePipeline()
    app = create_worker_app(load, pipeline, tmp_path / "scratch", token=TOKEN, port=39177)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
        assert (await client.get("/health")).status_code == 401
        assert (await client.put("/job", json=run.model_dump(mode="json"))).status_code == 401
        assert (
            await client.post("/status", json={"job_id": JOB, "attempt": 1, "fence": 2})
        ).status_code == 401
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        assert (await client.get("/openapi.json")).status_code == 404
        health = await client.get("/health")
        assert health.status_code == 200 and health.json()["state"] == "ready"
        wrong_model = run.model_copy(update={"instance_id": uuid.uuid4()})
        assert (
            await client.put("/job", json=wrong_model.model_dump(mode="json"))
        ).status_code == 409
        assert not (tmp_path / "scratch").exists()
        response = await client.put("/job", json=run.model_dump(mode="json"))
        assert response.status_code == 202
        assert ImageWorkerStatus.model_validate(response.json()).state == "running"
        replay = await client.put("/job", json=run.model_dump(mode="json"))
        assert replay.status_code == 200
        changed = run.model_copy(update={"deadline_at": run.deadline_at + timedelta(seconds=1)})
        assert (await client.put("/job", json=changed.model_dump(mode="json"))).status_code == 409
        other = run.model_copy(update={"fence": 3})
        assert (await client.put("/job", json=other.model_dump(mode="json"))).status_code == 409
        pipeline.release.set()
        done = await _wait_status(
            client, ImageJobBinding(job_id=JOB, attempt=1, fence=2), "generated"
        )
        assert pipeline.calls == 1
        assert len(done.outputs) == 1
        assert done.outputs[0].byte_count > 0
        assert done.outputs[0].sha256 != done.outputs[0].recipe_sha256
        assert "private" not in done.model_dump_json()
        assert (
            await client.post("/status", json={"job_id": JOB, "attempt": 1, "fence": 3})
        ).status_code == 404


async def test_cancel_reports_terminal_only_after_worker_stops(tmp_path: Path) -> None:
    load, run = _requests()
    pipeline = FakePipeline()
    app = create_worker_app(load, pipeline, tmp_path / "scratch", token=TOKEN, port=39178)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://worker", headers={"Authorization": f"Bearer {TOKEN}"}
    ) as client:
        assert (await client.put("/job", json=run.model_dump(mode="json"))).status_code == 202
        cancel = ImageWorkerCancelRequest(
            job_id=JOB, attempt=1, fence=2, requested_at=datetime.now(UTC)
        )
        first = await client.post("/cancel", json=cancel.model_dump(mode="json"))
        assert first.status_code == 202
        assert first.json()["state"] == "running"
        assert (
            await client.post("/cancel", json={**cancel.model_dump(mode="json"), "fence": 3})
        ).status_code == 404
        pipeline.release.set()
        done = await _wait_status(
            client, ImageJobBinding(job_id=JOB, attempt=1, fence=2), "cancelled"
        )
        assert done.outputs == ()


async def test_worker_attaches_classifier_result_within_held_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_node.image_runtime import control

    load, run = _requests()
    pipeline = FakePipeline()
    calls: list[Path] = []

    async def classify(
        model_dir: Path, image_path: Path, **kwargs: object
    ) -> ImageClassificationResult:
        assert model_dir == tmp_path / "classifier"
        assert kwargs["reservation_bytes"] == 256
        calls.append(image_path)
        return ImageClassificationResult(
            tag=ImageContentTag.EXPLICIT,
            score=Decimal("0.9"),
            classifier_revision=CLASSIFIER_REVISION,
            processor_sha256="a" * 64,
            tagged_at=datetime.now(UTC),
        )

    monkeypatch.setattr(control, "classify_image", classify)
    monkeypatch.setattr(control, "resident_bytes", lambda _pid: 512)
    app = create_worker_app(
        load,
        pipeline,
        tmp_path / "scratch",
        token=TOKEN,
        port=39179,
        classifier_model_dir=tmp_path / "classifier",
        classifier_memory_bytes=256,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://worker",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        assert (await client.put("/job", json=run.model_dump(mode="json"))).status_code == 202
        pipeline.release.set()
        done = await _wait_status(
            client, ImageJobBinding(job_id=JOB, attempt=1, fence=2), "generated"
        )
    assert len(calls) == 1
    assert done.outputs[0].classification is not None
    assert done.outputs[0].classification.tag is ImageContentTag.EXPLICIT


async def test_worker_skips_classifier_when_hold_is_too_small(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_node.image_runtime import control

    load, run = _requests()
    pipeline = FakePipeline()

    async def forbidden(*_args: object, **_kwargs: object) -> ImageClassificationResult:
        pytest.fail("classifier started without memory headroom")

    monkeypatch.setattr(control, "classify_image", forbidden)
    monkeypatch.setattr(control, "resident_bytes", lambda _pid: 800)
    app = create_worker_app(
        load,
        pipeline,
        tmp_path / "scratch",
        token=TOKEN,
        port=39180,
        classifier_model_dir=tmp_path / "classifier",
        classifier_memory_bytes=256,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://worker",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        assert (await client.put("/job", json=run.model_dump(mode="json"))).status_code == 202
        pipeline.release.set()
        done = await _wait_status(
            client, ImageJobBinding(job_id=JOB, attempt=1, fence=2), "generated"
        )
    assert done.outputs[0].classification is not None
    assert done.outputs[0].classification.safe_error == "classifier_memory"
