"""Real tiny mflux encoder, MLX denoiser, VAE decoder and Coire PNG recipe.

This runs only on a local Apple Silicon development Mac with the ignored fixture
from build_tiny_image_fixture.py. It does not contact either production Studio.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import runpy
import sys
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import httpx
import pytest
from PIL import Image
from pydantic import SecretStr

from coire_core.image_png import parse_recipe_png
from coire_core.models.image_worker import (
    ImageAssetValidateRequest,
    ImageTransferGrant,
    ImageTransferReceipt,
    ImageWorkerLoadRequest,
    ImageWorkerOutputManifest,
    ImageWorkerRunRequest,
    NodeImageCleanupRequest,
    NodeImageInputManifest,
    NodeImageStartRequest,
    NodeImageTransferRequest,
)
from coire_core.models.images import (
    ImageInputDigest,
    ImageMode,
    ImageSpec,
    ResolvedImageSpec,
    canonical_recipe_bytes,
    canonical_spec_hash,
    pixel_digest,
)
from coire_core.models.registry import ModelKind
from coire_core.settings import Settings
from coire_node.image_cleanup import cleanup_image_outputs
from coire_node.image_jobs import ImageJobJournal
from coire_node.image_runtime.pipeline import MfluxTxt2ImgPipeline
from coire_node.image_transfer import push_image_outputs
from coire_node.image_validation import validate_image_asset
from coire_node.image_worker import ImageJobCancelled, ImageJobExecutionError, run_image_job
from coire_node.store import Store

pytestmark = [
    pytest.mark.engine,
    pytest.mark.skipif(sys.platform != "darwin", reason="MLX requires local Apple Silicon"),
    pytest.mark.skipif(os.environ.get("COIRE_ENGINE") != "1", reason="set COIRE_ENGINE=1"),
]

JOB = "01J00000000000000000000000"


@pytest.fixture(scope="module")
def tiny_pipeline() -> Iterator[tuple[MfluxTxt2ImgPipeline, ImageWorkerLoadRequest]]:
    raw_path = os.environ.get("COIRE_TEST_MODEL")
    if not raw_path:
        pytest.fail("COIRE_TEST_MODEL must point to the built ignored tiny fixture")
    target = Path(raw_path).resolve()
    assert target.parent == (Path(__file__).resolve().parents[4] / "models").resolve()
    assert target.name.startswith("test--")
    store = Store(target.parent)
    manifest = store.read_manifest(target.name)
    assert manifest is not None and manifest.total_bytes <= 1_000_000_000
    namespace = runpy.run_path(str(Path(__file__).with_name("build_tiny_image_fixture.py")))
    tiny_models = cast(Callable[[], tuple[object, object, object]], namespace["tiny_models"])
    initializer = importlib.import_module("mflux.models.z_image.z_image_initializer")
    tokenizer_loader = importlib.import_module("mflux.models.common.tokenizer.tokenizer_loader")
    path_resolution = importlib.import_module("mflux.models.common.resolution.path_resolution")

    def initialize(model: object) -> None:
        vae, transformer, text_encoder = tiny_models()
        model.vae = vae  # type: ignore[attr-defined]
        model.transformer = transformer  # type: ignore[attr-defined]
        model.text_encoder = text_encoder  # type: ignore[attr-defined]

    def deny_fetch(*args: object, **kwargs: object) -> None:
        raise AssertionError("tiny engine attempted a model fetch")

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setenv("HF_HUB_OFFLINE", "1")
        patcher.setenv("TRANSFORMERS_OFFLINE", "1")
        patcher.delenv("HF_TOKEN", raising=False)
        patcher.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
        patcher.setattr(initializer.ZImageInitializer, "_init_models", staticmethod(initialize))
        patcher.setattr(tokenizer_loader, "snapshot_download", deny_fetch)
        patcher.setattr(path_resolution, "snapshot_download", deny_fetch)
        request = ImageWorkerLoadRequest(
            slug=target.name,
            model_id=uuid.uuid4(),
            instance_id=uuid.uuid4(),
            manifest_sha256=manifest.sha256(),
            reservation_bytes=500_000_000,
            runtime_version="mflux-0.20.0",
        )
        yield MfluxTxt2ImgPipeline.load(store, request), request


def _resolved(load: ImageWorkerLoadRequest) -> ResolvedImageSpec:
    spec = ImageSpec(
        model_id=load.model_id,
        prompt="a blue square",
        width=64,
        height=64,
        steps=1,
        guidance=Decimal(0),
        seed=1,
    )
    return ResolvedImageSpec(
        spec=spec,
        seeds=(1,),
        pipeline_version="mflux-0.20.0",
        environment_fingerprint="a" * 64,
        model_sha256=load.manifest_sha256,
        spec_hash=canonical_spec_hash(spec),
    )


def test_real_encoder_denoiser_decoder_and_recipe_round_trip(
    tiny_pipeline: tuple[MfluxTxt2ImgPipeline, ImageWorkerLoadRequest], tmp_path: Path
) -> None:
    pipeline, load = tiny_pipeline
    resolved = _resolved(load)
    scratch = tmp_path / "image-scratch"
    progress: list[tuple[int, int, int]] = []
    request = ImageWorkerRunRequest(
        job_id=JOB,
        attempt=1,
        fence=1,
        instance_id=load.instance_id,
        resolved=resolved,
        deadline_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    outputs = run_image_job(pipeline, load, request, scratch, lambda *parts: progress.append(parts))
    assert progress == [(0, 1, 1)]
    assert len(outputs) == 1
    output = outputs[0]
    recipe = parse_recipe_png(
        output.path, expected_size=output.encoded.byte_count, expected_sha256=output.encoded.sha256
    )
    assert recipe == output.encoded.recipe
    assert recipe.resolved == resolved and recipe.seed == 1
    with Image.open(output.path) as image:
        assert image.mode == "RGB" and image.size == (64, 64)
        assert recipe.pixel_sha256 == pixel_digest(image.tobytes(), width=64, height=64, channels=3)

    replay = run_image_job(
        pipeline,
        load,
        request.model_copy(update={"fence": 2}),
        scratch,
        lambda *_: None,
    )
    assert replay[0].encoded.recipe.pixel_sha256 == recipe.pixel_sha256


def test_real_step_cancellation_removes_partial_scratch(
    tiny_pipeline: tuple[MfluxTxt2ImgPipeline, ImageWorkerLoadRequest], tmp_path: Path
) -> None:
    pipeline, load = tiny_pipeline
    resolved = _resolved(load)
    scratch = tmp_path / "cancel-scratch"
    request = ImageWorkerRunRequest(
        job_id=JOB,
        attempt=1,
        fence=2,
        instance_id=load.instance_id,
        resolved=resolved,
        deadline_at=datetime.now(UTC) + timedelta(minutes=2),
    )

    def cancel(index: int, step: int, total: int) -> None:
        assert (index, step, total) == (0, 1, 1)
        raise ImageJobCancelled()

    with pytest.raises(ImageJobExecutionError):
        run_image_job(pipeline, load, request, scratch, cancel)
    assert not (scratch / f"{JOB}-1-2").exists()


def test_real_img2img_uses_bound_staged_png_and_exact_strength(
    tiny_pipeline: tuple[MfluxTxt2ImgPipeline, ImageWorkerLoadRequest], tmp_path: Path
) -> None:
    pipeline, load = tiny_pipeline
    input_id = uuid.uuid4()
    source = tmp_path / "image-input-scratch" / f"{JOB}-1-3" / str(input_id)
    source.parent.mkdir(parents=True, mode=0o700)
    source.parent.parent.chmod(0o700)
    Image.new("RGB", (64, 64), "blue").save(source, format="PNG")
    source.chmod(0o600)
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    spec = _resolved(load).spec.model_copy(
        update={
            "mode": ImageMode.IMG2IMG,
            "init_image_id": input_id,
            "strength": Decimal("0.375125"),
            "steps": 4,
        }
    )
    resolved = _resolved(load).model_copy(
        update={
            "spec": spec,
            "spec_hash": canonical_spec_hash(spec),
            "inputs": (ImageInputDigest(input_id=input_id, sha256=digest, width=64, height=64),),
        }
    )
    command = ImageWorkerRunRequest(
        job_id=JOB,
        attempt=1,
        fence=3,
        instance_id=load.instance_id,
        resolved=resolved,
        inputs=(
            NodeImageInputManifest(
                input_id=input_id,
                purpose="init",
                sha256=digest,
                byte_count=len(payload),
                width=64,
                height=64,
            ),
        ),
        deadline_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    progress: list[tuple[int, int, int]] = []
    outputs = run_image_job(
        pipeline, load, command, tmp_path / "image-scratch", lambda *parts: progress.append(parts)
    )
    assert progress[0] == (0, 2, 4) and progress[-1] == (0, 4, 4)
    assert all(index == 0 and total == 4 for index, _, total in progress)
    assert [step for _, step, _ in progress] == sorted({step for _, step, _ in progress})
    assert outputs[0].encoded.recipe.resolved == resolved
    with Image.open(outputs[0].path) as image:
        assert image.mode == "RGB" and image.size == (64, 64)
    changed = tmp_path / "image-input-scratch" / f"{JOB}-1-4"
    changed.mkdir(mode=0o700)
    (changed / str(input_id)).write_bytes(payload + b"changed")
    (changed / str(input_id)).chmod(0o600)
    with pytest.raises(ImageJobExecutionError):
        run_image_job(
            pipeline,
            load,
            command.model_copy(update={"fence": 4}),
            tmp_path / "image-scratch",
            lambda *_: None,
        )
    assert not (tmp_path / "image-scratch" / f"{JOB}-1-4").exists()


def test_real_acquisition_smoke_proves_txt2img_and_img2img(
    tiny_pipeline: tuple[MfluxTxt2ImgPipeline, ImageWorkerLoadRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline, load = tiny_pipeline
    store = Store(Path(os.environ["COIRE_TEST_MODEL"]).resolve().parent)
    manifest = store.read_manifest(load.slug)
    assert manifest is not None
    monkeypatch.setattr(MfluxTxt2ImgPipeline, "load", lambda *_: pipeline)
    result = validate_image_asset(
        store,
        ImageAssetValidateRequest(
            job_id=uuid.uuid4(),
            model_id=load.model_id,
            slug=load.slug,
            kind=ModelKind.IMAGE_MODEL,
            source_revision=manifest.revision,
            manifest_sha256=load.manifest_sha256,
            reservation_id=uuid.uuid4(),
        ),
        reservation_bytes=load.reservation_bytes,
    )
    assert result.validated and result.thumbnail_sha256 is not None
    assert result.image_capability_profile is not None
    assert result.image_capability_profile.modes == (ImageMode.TXT2IMG, ImageMode.IMG2IMG)


def test_real_encoder_cache_twenty_warm_trials_and_changed_prompt_miss(
    tiny_pipeline: tuple[MfluxTxt2ImgPipeline, ImageWorkerLoadRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline, load = tiny_pipeline
    encoder_module = importlib.import_module(
        "mflux.models.z_image.model.z_image_text_encoder.prompt_encoder"
    )
    original = encoder_module.PromptEncoder.encode_prompt
    calls = 0
    cache_events: list[tuple[str, str]] = []

    def record_cache(stage: str, outcome: str, occupancy: int) -> None:
        assert 0 <= occupancy <= pipeline.encoder_cache.max_bytes
        cache_events.append((stage, outcome))

    monkeypatch.setattr("coire_node.image_runtime.cache.record_image_cache", record_cache)

    def count_encode(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(encoder_module.PromptEncoder, "encode_prompt", staticmethod(count_encode))
    base = _resolved(load)
    changed = base.spec.model_copy(update={"prompt": "a green square for cache evidence"})
    resolved = base.model_copy(update={"spec": changed, "spec_hash": canonical_spec_hash(changed)})
    digests: list[str] = []
    for _ in range(21):
        images = pipeline.generate(resolved, lambda *_: None)
        try:
            digests.append(pixel_digest(images[0].tobytes(), width=64, height=64, channels=3))
        finally:
            for image in images:
                image.close()
    assert len(set(digests)) == 1
    assert calls == 1
    assert cache_events.count(("prompt", "hit")) == 20
    assert cache_events.count(("prompt", "miss")) == 1
    assert cache_events.count(("prompt", "store")) == 1
    assert 0 < pipeline.encoder_cache.used_bytes <= pipeline.encoder_cache.max_bytes
    other = base.spec.model_copy(update={"prompt": "a red square for cache evidence"})
    other_resolved = base.model_copy(
        update={"spec": other, "spec_hash": canonical_spec_hash(other)}
    )
    for image in pipeline.generate(other_resolved, lambda *_: None):
        image.close()
    assert calls == 2
    assert cache_events.count(("prompt", "miss")) == 2
    changed_environment = other_resolved.model_copy(update={"environment_fingerprint": "b" * 64})
    for image in pipeline.generate(changed_environment, lambda *_: None):
        image.close()
    assert calls == 3
    assert cache_events.count(("prompt", "miss")) == 3


async def test_real_png_transfer_receipt_allows_node_scratch_cleanup(
    tiny_pipeline: tuple[MfluxTxt2ImgPipeline, ImageWorkerLoadRequest], tmp_path: Path
) -> None:
    pipeline, load = tiny_pipeline
    resolved = _resolved(load)
    node = "coire-edge-b"
    deadline = datetime.now(UTC) + timedelta(minutes=2)
    start = NodeImageStartRequest(
        job_id=JOB,
        attempt=1,
        fence=4,
        node=node,
        model_id=load.model_id,
        instance_id=load.instance_id,
        resolved=resolved,
        deadline_at=deadline,
        reservation_bytes=load.reservation_bytes,
    )
    settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        node_token=SecretStr("node-secret"),
        node_state_dir=str(tmp_path),
    )
    scratch = tmp_path / "image-scratch"
    output = run_image_job(
        pipeline,
        load,
        ImageWorkerRunRequest(
            job_id=JOB,
            attempt=1,
            fence=4,
            instance_id=load.instance_id,
            resolved=resolved,
            deadline_at=deadline,
        ),
        scratch,
        lambda *_: None,
    )[0]
    journal = ImageJobJournal(tmp_path, node)
    current = journal.begin(start)
    current = journal.advance(
        current.model_copy(
            update={
                "state": "reserving",
                "pid": os.getpid(),
                "process_create_time": 1.0,
                "updated_at": current.updated_at + timedelta(microseconds=1),
            }
        )
    )
    current = journal.advance(
        current.model_copy(
            update={
                "state": "running",
                "updated_at": current.updated_at + timedelta(microseconds=1),
            }
        )
    )
    manifest = ImageWorkerOutputManifest(
        index=0,
        byte_count=output.encoded.byte_count,
        sha256=output.encoded.sha256,
        recipe_sha256=hashlib.sha256(canonical_recipe_bytes(output.encoded.recipe)).hexdigest(),
    )
    current = journal.advance(
        current.model_copy(
            update={
                "state": "transferring",
                "outputs": (manifest,),
                "updated_at": current.updated_at + timedelta(microseconds=1),
            }
        )
    )
    issued = datetime.now(UTC)
    grant = ImageTransferGrant(
        job_id=JOB,
        attempt=1,
        fence=4,
        node=node,
        index=0,
        expected_bytes=manifest.byte_count,
        expected_sha256=manifest.sha256,
        token="transfer-token",
        issued_at=issued,
        expires_at=issued + timedelta(minutes=1),
    )
    receipt = ImageTransferReceipt(
        job_id=JOB,
        attempt=1,
        fence=4,
        node=node,
        index=0,
        output_id=uuid.uuid4(),
        byte_count=manifest.byte_count,
        sha256=manifest.sha256,
        recipe_sha256=manifest.recipe_sha256,
        verified_at=datetime.now(UTC),
    )

    async def receive(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-coire-transfer-grant"] == "transfer-token"
        assert request.content == output.path.read_bytes()
        return httpx.Response(201, json=receipt.model_dump(mode="json"))

    transferred = await push_image_outputs(
        tmp_path,
        current,
        start,
        NodeImageTransferRequest(job_id=JOB, attempt=1, fence=4, node=node, grants=(grant,)),
        settings,
        transport=httpx.MockTransport(receive),
    )
    assert transferred == (receipt,)
    cleaned = cleanup_image_outputs(
        journal,
        tmp_path,
        NodeImageCleanupRequest(job_id=JOB, attempt=1, fence=4, node=node, receipts=transferred),
    )
    assert cleaned.state == "cleaned"
    assert not output.path.exists()
    assert journal.get(JOB).scratch_cleaned is True  # type: ignore[union-attr]
