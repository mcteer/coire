"""Reserved image validation requires exact offline bytes and a real pixel smoke."""

from __future__ import annotations

import importlib
import json
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import psutil
import pytest
from PIL import Image, ImageDraw

from coire_core.models.image_worker import (
    ImageAssetValidateRequest,
    ImageAssetValidationResult,
    ImageValidationBase,
    ImageWorkerLoadRequest,
)
from coire_core.models.images import (
    ImageCapabilityProfile,
    ImageClassificationResult,
    ImageContentTag,
    ImageMode,
    ResolvedImageSpec,
)
from coire_core.models.jobs import JobKind, JobStage, JobStatus
from coire_core.models.registry import ModelKind
from coire_node import image_validation
from coire_node.image_runtime import pipeline
from coire_node.image_runtime.classification import CLASSIFIER_REVISION
from coire_node.image_runtime.preflight import ImageCopyUnavailable
from coire_node.image_validation import ImageValidationUnavailable, validate_image_asset
from coire_node.store import Store
from coire_node.worker import EXIT_OK, JobFile, run_image_validate

REVISION = "a" * 40


def test_fill_smoke_validates_mask_inputs_and_publishes_fill_only_profile() -> None:
    class FakeFill:
        def generate(
            self,
            resolved: ResolvedImageSpec,
            progress: Callable[[int, int, int], None],
            *,
            input_paths: dict[uuid.UUID, Path] | None = None,
        ) -> tuple[Image.Image, ...]:
            assert resolved.spec.mode is ImageMode.FILL
            assert resolved.spec.guidance == 4
            assert input_paths is not None and len(input_paths) == 2
            assert resolved.spec.mask_id is not None
            mask = input_paths[resolved.spec.mask_id]
            with Image.open(mask) as image:
                assert image.mode == "L" and image.getpixel((256, 256)) == 255
                assert image.getpixel((0, 0)) == 0
            progress(0, 4, 4)
            output = Image.new("RGB", (512, 512), "blue")
            ImageDraw.Draw(output).rectangle((128, 128, 384, 384), fill="white")
            return (output,)

    result = image_validation._validate_fill_smoke(
        cast(Any, FakeFill()),
        _request("a" * 64),
        lambda: None,
        lambda: (1000, 1000),
        baseline_physical=100,
        reservation_bytes=2000,
    )
    assert result.image_capability_profile is not None
    assert result.image_capability_profile.modes == (ImageMode.FILL,)
    assert result.image_capability_profile.max_loras == 0
    with pytest.raises(ImageValidationUnavailable, match="reserved memory"):
        image_validation._validate_fill_smoke(
            cast(Any, FakeFill()),
            _request("a" * 64),
            lambda: None,
            lambda: (3000, 3000),
            baseline_physical=100,
            reservation_bytes=2000,
        )


def test_fill_validation_refuses_transient_load_peak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request("a" * 64)
    hold = psutil.Process().memory_info().rss + 1_000_000
    load = ImageWorkerLoadRequest(
        slug=request.slug,
        model_id=request.model_id,
        instance_id=request.job_id,
        manifest_sha256=request.manifest_sha256,
        reservation_bytes=hold,
        runtime_version="mflux-0.20.0",
    )
    loading = threading.Event()
    monkeypatch.setattr(
        image_validation, "resident_bytes", lambda _pid: hold + 1 if loading.is_set() else 100
    )

    def load_model(*_: object) -> object:
        loading.set()
        time.sleep(0.05)
        loading.clear()
        return object()

    monkeypatch.setattr(pipeline.MfluxTxt2ImgPipeline, "load", load_model)
    monkeypatch.setattr(
        image_validation,
        "_validate_fill_smoke",
        lambda *_args, **_kwargs: ImageAssetValidationResult(
            validated=False,
            kind=ModelKind.IMAGE_MODEL,
            manifest_sha256=request.manifest_sha256,
            source_revision=request.source_revision,
            peak_rss_bytes=100,
        ),
    )
    with pytest.raises(ImageValidationUnavailable, match="exceeded reserved memory"):
        image_validation._validate_fill_with_peak(
            Store(tmp_path), load, request, baseline_physical=100, reservation_bytes=hold
        )


def _copy(tmp_path: Path, *, revision: str = REVISION) -> tuple[Store, str]:
    store = Store(tmp_path / "models")
    slug = "org--image"
    root = store.path_for(slug)
    root.mkdir(parents=True)
    (root / "config.json").write_text("{}")
    (root / "model.safetensors").write_bytes(b"weights")
    manifest = store.hash_tree(slug, repo_id="org/image", revision=revision)
    store.write_manifest(manifest)
    return store, manifest.sha256()


def _request(
    digest: str, *, kind: ModelKind = ModelKind.IMAGE_MODEL, revision: str = REVISION
) -> ImageAssetValidateRequest:
    return ImageAssetValidateRequest(
        job_id=uuid.uuid4(),
        model_id=uuid.uuid4(),
        slug="org--image",
        kind=kind,
        source_revision=revision,
        manifest_sha256=digest,
        reservation_id=uuid.uuid4(),
        compatible_base=(
            ImageValidationBase(
                model_id=uuid.uuid4(),
                slug="org--image",
                source_revision=revision,
                manifest_sha256=digest,
            )
            if kind in {ModelKind.IMAGE_LORA, ModelKind.CONTROL_MODEL}
            else None
        ),
    )


def test_reserved_base_smoke_produces_narrow_capability_and_thumbnail_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, digest = _copy(tmp_path)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.delenv("HF_TOKEN", raising=False)

    class FakePipeline:
        def generate(
            self,
            resolved: ResolvedImageSpec,
            progress: Callable[[int, int, int], None],
            *,
            input_paths: dict[uuid.UUID, Path] | None = None,
        ) -> tuple[Image.Image, ...]:
            if input_paths is not None:
                assert resolved.spec.mode == "img2img"
                assert len(input_paths) == 1
            image = Image.new("RGB", (512, 512), "blue")
            ImageDraw.Draw(image).rectangle((128, 128, 384, 384), fill="white")
            return (image,)

    monkeypatch.setattr(
        pipeline.MfluxTxt2ImgPipeline,
        "load",
        lambda *args: FakePipeline(),
    )
    result = validate_image_asset(store, _request(digest), reservation_bytes=10**9)
    assert result.validated
    assert result.thumbnail_sha256 is not None
    assert result.image_capability_profile is not None
    assert result.image_capability_profile.modes == ("txt2img", "img2img")
    assert result.image_capability_profile.max_width == 512
    assert result.image_capability_profile.max_outputs == 1
    assert result.peak_physical_delta_bytes is not None
    assert result.peak_physical_bytes is not None
    assert result.peak_physical_bytes >= result.peak_physical_delta_bytes
    assert result.peak_rss_bytes > 0


def test_validation_refuses_transient_physical_footprint_above_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, digest = _copy(tmp_path)
    # The 301-byte delta fits the hold, but the 1201-byte process peak does not.
    samples = iter((900, 950, 1201, 950, 950, 950))
    monkeypatch.setattr(image_validation, "resident_bytes", lambda _: next(samples))

    class FakePipeline:
        def generate(
            self,
            resolved: ResolvedImageSpec,
            progress: Callable[[int, int, int], None],
            *,
            input_paths: dict[uuid.UUID, Path] | None = None,
        ) -> tuple[Image.Image, ...]:
            del resolved, input_paths
            progress(0, 1, 4)
            image = Image.new("RGB", (512, 512), "blue")
            ImageDraw.Draw(image).rectangle((128, 128, 384, 384), fill="white")
            return (image,)

    monkeypatch.setattr(pipeline.MfluxTxt2ImgPipeline, "load", lambda *args: FakePipeline())
    with pytest.raises(ImageValidationUnavailable, match="exceeded reserved memory"):
        validate_image_asset(store, _request(digest), reservation_bytes=1000)


def test_validation_rejects_changed_manifest_or_extra_local_file_before_native_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, digest = _copy(tmp_path)
    monkeypatch.setattr(
        pipeline.MfluxTxt2ImgPipeline,
        "load",
        lambda *args: pytest.fail("native load must not run"),
    )
    with pytest.raises(ImageValidationUnavailable, match="manifest differs"):
        validate_image_asset(store, _request("0" * 64), reservation_bytes=10**9)
    (store.path_for("org--image") / "extra.json").write_text("{}")
    with pytest.raises(ImageCopyUnavailable):
        validate_image_asset(store, _request(digest), reservation_bytes=10**9)


def test_control_refuses_unreviewed_repository_layout(tmp_path: Path) -> None:
    store, digest = _copy(tmp_path)
    with pytest.raises(ImageValidationUnavailable, match="unsupported local control layout"):
        validate_image_asset(
            store, _request(digest, kind=ModelKind.CONTROL_MODEL), reservation_bytes=10**9
        )


def test_control_smoke_uses_exact_local_composite_without_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, base_digest = _copy(tmp_path)
    control_slug = "alibaba-pai--Z-Image-Turbo-Fun-Controlnet-Union-2.1"
    root = store.path_for(control_slug)
    root.mkdir()
    checkpoint = "Z-Image-Turbo-Fun-Controlnet-Union-2.1.safetensors"
    (root / checkpoint).write_bytes(b"control weights")
    manifest = store.hash_tree(
        control_slug,
        repo_id="alibaba-pai/Z-Image-Turbo-Fun-Controlnet-Union-2.1",
        revision=REVISION,
    )
    store.write_manifest(manifest)
    base = ImageValidationBase(
        model_id=uuid.uuid4(),
        slug="org--image",
        source_revision=REVISION,
        manifest_sha256=base_digest,
    )
    request = ImageAssetValidateRequest(
        job_id=uuid.uuid4(),
        model_id=uuid.uuid4(),
        slug=control_slug,
        kind=ModelKind.CONTROL_MODEL,
        source_revision=REVISION,
        manifest_sha256=manifest.sha256(),
        reservation_id=uuid.uuid4(),
        compatible_base=base,
    )
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    called: list[Path] = []

    class FakeControl:
        def __init__(self, *, model_path: str, model_config: object) -> None:
            composite = Path(model_path)
            assert model_config == "control-union"
            assert (composite / "model.safetensors").read_bytes() == b"weights"
            assert (composite / "controlnet" / checkpoint).read_bytes() == b"control weights"
            called.append(composite)

        def parameters(self) -> tuple[()]:
            return ()

        def generate_image(self, **kwargs: object) -> Image.Image:
            assert kwargs["num_inference_steps"] == 4
            image = Image.new("RGB", (512, 512), "blue")
            ImageDraw.Draw(image).rectangle((128, 128, 384, 384), fill="white")
            return image

    def module(name: str) -> object:
        if name.endswith("z_image_turbo_controlnet"):
            return SimpleNamespace(ZImageTurboControlnet=FakeControl)
        if name.endswith("control_types"):
            return SimpleNamespace(
                ControlSpec=lambda **kwargs: kwargs,
                ControlType=SimpleNamespace(canny="canny"),
            )
        if name.endswith("model_config"):
            return SimpleNamespace(
                ModelConfig=SimpleNamespace(
                    z_image_turbo_controlnet_union_2_1=lambda: "control-union"
                )
            )
        if name == "mlx.core":
            return SimpleNamespace(eval=lambda value: None)
        raise AssertionError(name)

    monkeypatch.setattr(importlib, "import_module", module)
    result = validate_image_asset(store, request, reservation_bytes=10**12)
    assert result.validated and result.thumbnail_sha256 is not None
    assert len(called) == 1 and not called[0].exists()
    with pytest.raises(ImageCopyUnavailable):
        validate_image_asset(
            store,
            request.model_copy(
                update={"compatible_base": base.model_copy(update={"manifest_sha256": "0" * 64})}
            ),
            reservation_bytes=10**12,
        )
    assert len(called) == 1


def test_lora_smoke_uses_one_verified_local_adapter_and_exact_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "models")
    for slug, filename in (
        ("org--base", "model.safetensors"),
        ("org--adapter", "adapter.safetensors"),
    ):
        root = store.path_for(slug)
        root.mkdir(parents=True)
        (root / filename).write_bytes(slug.encode())
        store.write_manifest(
            store.hash_tree(slug, repo_id=slug.replace("--", "/"), revision=REVISION)
        )
    base_manifest = store.read_manifest("org--base")
    adapter_manifest = store.read_manifest("org--adapter")
    assert base_manifest is not None and adapter_manifest is not None
    base = ImageValidationBase(
        model_id=uuid.uuid4(),
        slug="org--base",
        source_revision=REVISION,
        manifest_sha256=base_manifest.sha256(),
    )
    request = ImageAssetValidateRequest(
        job_id=uuid.uuid4(),
        model_id=uuid.uuid4(),
        slug="org--adapter",
        kind=ModelKind.IMAGE_LORA,
        source_revision=REVISION,
        manifest_sha256=adapter_manifest.sha256(),
        reservation_id=uuid.uuid4(),
        compatible_base=base,
    )
    loaded: list[Path] = []

    def load_native(
        path: Path, *, lora_paths: tuple[Path, ...], lora_scales: tuple[float, ...]
    ) -> object:
        assert path == store.path_for(base.slug)
        assert lora_paths == (store.path_for(request.slug) / "adapter.safetensors",)
        assert lora_scales == (1.0,)
        loaded.extend(lora_paths)
        return object()

    class FakePipeline:
        def __init__(self, model: object, load: object) -> None:
            assert model is not None and load is not None

        def generate(
            self, resolved: ResolvedImageSpec, progress: Callable[[int, int, int], None]
        ) -> tuple[Image.Image, ...]:
            assert resolved.spec.model_id == base.model_id
            progress(0, 4, 4)
            image = Image.new("RGB", (512, 512), "blue")
            ImageDraw.Draw(image).rectangle((128, 128, 384, 384), fill="white")
            return (image,)

    monkeypatch.setattr(image_validation, "_load_native", load_native)
    monkeypatch.setattr(image_validation, "MfluxTxt2ImgPipeline", FakePipeline)
    result = validate_image_asset(store, request, reservation_bytes=10**12)
    assert result.validated and result.kind is ModelKind.IMAGE_LORA
    assert result.thumbnail_sha256 is not None and loaded
    with pytest.raises(ImageCopyUnavailable):
        validate_image_asset(
            store,
            request.model_copy(
                update={"compatible_base": base.model_copy(update={"manifest_sha256": "0" * 64})}
            ),
            reservation_bytes=10**12,
        )
    assert len(loaded) == 1


def test_seedvr2_upscale_smoke_requires_exact_reviewed_local_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "models")
    slug = "numz--SeedVR2_comfyUI"
    root = store.path_for(slug)
    root.mkdir(parents=True)
    for filename in ("seedvr2_ema_3b_fp16.safetensors", "ema_vae_fp16.safetensors"):
        (root / filename).write_bytes(filename.encode())
    manifest = store.hash_tree(slug, repo_id="numz/SeedVR2_comfyUI", revision=REVISION)
    store.write_manifest(manifest)
    request = ImageAssetValidateRequest(
        job_id=uuid.uuid4(),
        model_id=uuid.uuid4(),
        slug=slug,
        kind=ModelKind.UPSCALE_MODEL,
        source_revision=REVISION,
        manifest_sha256=manifest.sha256(),
        reservation_id=uuid.uuid4(),
    )
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    imports: list[str] = []

    class FakeSeedVR2:
        def __init__(self, *, model_path: str, model_config: object) -> None:
            assert model_path == str(root) and model_config == "seedvr2-3b"

        def parameters(self) -> tuple[()]:
            return ()

        def generate_image(self, *, seed: int, image_path: Path, resolution: int) -> object:
            assert seed == 1 and resolution == 128 and image_path.is_file()
            image = Image.new("RGB", (128, 128), "blue")
            ImageDraw.Draw(image).rectangle((32, 32, 96, 96), fill="white")
            return SimpleNamespace(image=image)

    def module(name: str) -> object:
        imports.append(name)
        if name.endswith("variants.upscale.seedvr2"):
            return SimpleNamespace(SeedVR2=FakeSeedVR2)
        if name.endswith("model_config"):
            return SimpleNamespace(ModelConfig=SimpleNamespace(seedvr2_3b=lambda: "seedvr2-3b"))
        if name == "mlx.core":
            return SimpleNamespace(eval=lambda value: None)
        raise AssertionError(name)

    monkeypatch.setattr(importlib, "import_module", module)
    result = validate_image_asset(store, request, reservation_bytes=10**12)
    assert result.validated and result.thumbnail_sha256 is not None
    assert any(name.endswith("variants.upscale.seedvr2") for name in imports)
    (root / "seedvr2_ema_7b_fp16.safetensors").write_bytes(b"unexpected")
    with pytest.raises(ImageCopyUnavailable):
        validate_image_asset(store, request, reservation_bytes=10**12)
    assert len(imports) == 3


def test_published_base_evidence_requires_physical_peak() -> None:
    profile = ImageCapabilityProfile(
        modes=(ImageMode.TXT2IMG,),
        min_width=512,
        max_width=512,
        min_height=512,
        max_height=512,
        max_pixels=512 * 512,
        min_steps=4,
        max_steps=4,
        min_guidance=Decimal(0),
        max_guidance=Decimal(0),
        max_outputs=1,
    )
    with pytest.raises(ValueError, match="physical evidence"):
        ImageAssetValidationResult(
            validated=True,
            kind=ModelKind.IMAGE_MODEL,
            manifest_sha256="b" * 64,
            source_revision=REVISION,
            peak_rss_bytes=100,
            thumbnail_sha256="c" * 64,
            image_capability_profile=profile,
        )


def test_pinned_classifier_requires_local_cpu_smoke_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, digest = _copy(tmp_path, revision=CLASSIFIER_REVISION)
    called: list[Path] = []

    async def classify(
        model_dir: Path, image_path: Path, **kwargs: object
    ) -> tuple[ImageClassificationResult, int]:
        assert model_dir == store.path_for("org--image")
        assert kwargs["job_id"] is None
        called.append(image_path)
        result = ImageClassificationResult(
            tag=ImageContentTag.NORMAL,
            score=Decimal("0.1"),
            threshold=Decimal("0.5"),
            classifier_revision=CLASSIFIER_REVISION,
            processor_sha256="c" * 64,
            tagged_at=datetime.now(UTC),
        )
        return result, 1234

    monkeypatch.setattr(image_validation, "classify_image_with_peak", classify)
    result = validate_image_asset(
        store,
        _request(digest, kind=ModelKind.IMAGE_CLASSIFIER, revision=CLASSIFIER_REVISION),
        reservation_bytes=10**9,
    )
    assert result.validated and result.kind is ModelKind.IMAGE_CLASSIFIER
    assert result.image_capability_profile is None and result.thumbnail_sha256 is None
    assert result.peak_rss_bytes == 1234
    assert len(called) == 1 and not called[0].exists()


def test_classifier_wrong_revision_or_unknown_evidence_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, digest = _copy(tmp_path)

    async def unexpected(*args: object, **kwargs: object) -> tuple[ImageClassificationResult, int]:
        pytest.fail("wrong revision must not execute classifier")

    monkeypatch.setattr(image_validation, "classify_image_with_peak", unexpected)
    with pytest.raises(ImageValidationUnavailable, match="revision differs"):
        validate_image_asset(
            store, _request(digest, kind=ModelKind.IMAGE_CLASSIFIER), reservation_bytes=10**9
        )
    pinned_store, pinned_digest = _copy(tmp_path / "pinned", revision=CLASSIFIER_REVISION)

    async def unknown(*args: object, **kwargs: object) -> tuple[ImageClassificationResult, int]:
        result = ImageClassificationResult(
            tag=ImageContentTag.UNKNOWN,
            threshold=Decimal("0.5"),
            classifier_revision=CLASSIFIER_REVISION,
            safe_error="classifier_failed",
            tagged_at=datetime.now(UTC),
        )
        return result, 200

    monkeypatch.setattr(image_validation, "classify_image_with_peak", unknown)
    with pytest.raises(ImageValidationUnavailable, match="invalid evidence"):
        validate_image_asset(
            pinned_store,
            _request(
                pinned_digest,
                kind=ModelKind.IMAGE_CLASSIFIER,
                revision=CLASSIFIER_REVISION,
            ),
            reservation_bytes=10**9,
        )


def test_acquisition_worker_persists_typed_validation_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, digest = _copy(tmp_path)
    now = datetime.now(UTC)
    status = JobStatus(
        job_id=uuid.uuid4(),
        kind=JobKind.IMAGE_VALIDATE,
        slug="org--image",
        stage=JobStage.QUEUED,
        started_at=now,
        updated_at=now,
    )
    path = tmp_path / "job.json"
    path.write_text(
        json.dumps(
            {
                "status": status.model_dump(mode="json"),
                "params": {
                    "store_dir": str(tmp_path / "models"),
                    "model_id": str(uuid.uuid4()),
                    "model_kind": "image_model",
                    "source_revision": REVISION,
                    "manifest_sha256": digest,
                    "reservation_id": str(uuid.uuid4()),
                    "reservation_bytes": 10**9,
                },
            }
        )
    )
    evidence = ImageAssetValidationResult(
        validated=True,
        kind=ModelKind.IMAGE_MODEL,
        manifest_sha256=digest,
        source_revision=REVISION,
        peak_rss_bytes=100,
        peak_physical_bytes=200,
        peak_physical_delta_bytes=100,
        thumbnail_sha256="c" * 64,
        image_capability_profile=ImageCapabilityProfile.model_validate(
            {
                "modes": ["txt2img"],
                "min_width": 512,
                "max_width": 512,
                "min_height": 512,
                "max_height": 512,
                "max_pixels": 512 * 512,
                "min_steps": 4,
                "max_steps": 4,
                "min_guidance": 0,
                "max_guidance": 0,
                "max_outputs": 1,
            }
        ),
    )
    monkeypatch.setattr(
        "coire_node.image_validation.validate_image_asset", lambda *args, **kwargs: evidence
    )
    job = JobFile(path)
    assert run_image_validate(job) == EXIT_OK
    assert job.status.stage is JobStage.DONE
    assert ImageAssetValidationResult.model_validate(job.status.result) == evidence
