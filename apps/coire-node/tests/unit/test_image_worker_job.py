"""A fenced image attempt owns private scratch and content-free progress."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image

from coire_core.models.image_worker import ImageWorkerLoadRequest, ImageWorkerRunRequest
from coire_core.models.images import ImageSpec, ResolvedImageSpec, canonical_spec_hash
from coire_file_worker.image_inputs import parse_recipe_png
from coire_node import image_worker
from coire_node.image_runtime.metadata import write_image_png

JOB = "01J00000000000000000000000"


class FakePipeline:
    def __init__(self) -> None:
        self.called = False
        self.images: list[Image.Image] = []

    def generate(
        self, resolved: ResolvedImageSpec, callback: image_worker.Progress
    ) -> tuple[Image.Image, ...]:
        self.called = True
        for index in range(resolved.spec.n):
            for step in range(1, resolved.spec.steps + 1):
                callback(index, step, resolved.spec.steps)
            self.images.append(Image.new("RGB", (64, 64), (index, 0, 0)))
        return tuple(self.images)


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
        steps=4,
        guidance=Decimal(0),
        seed=7,
        n=2,
    )
    resolved = ResolvedImageSpec(
        spec=spec,
        seeds=(7, 8),
        pipeline_version=load.runtime_version,
        environment_fingerprint="b" * 64,
        model_sha256=load.manifest_sha256,
        spec_hash=canonical_spec_hash(spec),
    )
    run = ImageWorkerRunRequest(
        job_id=JOB,
        attempt=1,
        fence=2,
        instance_id=load.instance_id,
        resolved=resolved,
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    return load, run


def test_success_writes_private_fenced_outputs_and_final_progress(tmp_path: Path) -> None:
    load, run = _requests()
    pipeline = FakePipeline()
    progress: list[tuple[int, int, int]] = []
    clock = iter([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7])
    outputs = image_worker.run_image_job(
        pipeline,
        load,
        run,
        tmp_path / "scratch",
        lambda index, step, total: progress.append((index, step, total)),
        monotonic=lambda: next(clock),
    )
    assert [output.index for output in outputs] == [0, 1]
    assert progress == [(0, 1, 4), (0, 4, 4), (1, 3, 4), (1, 4, 4)]
    assert all(output.path.parent.name == f"{JOB}-1-2" for output in outputs)
    assert all(output.path.stat().st_mode & 0o777 == 0o600 for output in outputs)
    assert outputs[0].path.parent.stat().st_mode & 0o777 == 0o700
    assert outputs[0].path.parent.parent.stat().st_mode & 0o777 == 0o700
    assert [parse_recipe_png(output.path) for output in outputs] == [
        output.encoded.recipe for output in outputs
    ]


def test_replay_and_binding_mismatch_do_not_overwrite(tmp_path: Path) -> None:
    load, run = _requests()
    pipeline = FakePipeline()
    scratch = tmp_path / "scratch"
    first = image_worker.run_image_job(pipeline, load, run, scratch, lambda *args: None)
    content = first[0].path.read_bytes()
    with pytest.raises(image_worker.ImageJobExecutionError):
        image_worker.run_image_job(FakePipeline(), load, run, scratch, lambda *args: None)
    assert first[0].path.read_bytes() == content
    other = run.model_copy(update={"instance_id": uuid.uuid4()})
    with pytest.raises(image_worker.ImageJobExecutionError):
        image_worker.run_image_job(
            FakePipeline(), load, other, tmp_path / "wrong", lambda *args: None
        )
    assert not (tmp_path / "wrong").exists()


def test_failure_removes_all_partial_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    load, run = _requests()
    pipeline = FakePipeline()
    writer = write_image_png
    calls = 0

    def fail_second(
        image: Image.Image, resolved: ResolvedImageSpec, index: int, path: Path
    ) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("private failure")
        return writer(image, resolved, index, path)

    monkeypatch.setattr(image_worker, "write_image_png", fail_second)
    with pytest.raises(image_worker.ImageJobExecutionError, match="image job failed"):
        image_worker.run_image_job(pipeline, load, run, tmp_path / "scratch", lambda *args: None)
    assert not (tmp_path / "scratch" / f"{JOB}-1-2").exists()


def test_expired_deadline_and_symlink_scratch_fail_closed(tmp_path: Path) -> None:
    load, run = _requests()
    expired = run.model_copy(update={"deadline_at": datetime.now(UTC) - timedelta(seconds=1)})
    with pytest.raises(image_worker.ImageJobExecutionError):
        image_worker.run_image_job(
            FakePipeline(), load, expired, tmp_path / "scratch", lambda *args: None
        )
    assert not (tmp_path / "scratch").exists()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(private, target_is_directory=True)
    with pytest.raises(image_worker.ImageJobExecutionError):
        image_worker.run_image_job(FakePipeline(), load, run, linked, lambda *args: None)
    assert list(private.iterdir()) == []
