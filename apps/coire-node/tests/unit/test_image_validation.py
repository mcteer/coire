"""Reserved image validation requires exact offline bytes and a real pixel smoke."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from coire_core.models.image_worker import ImageAssetValidateRequest, ImageAssetValidationResult
from coire_core.models.images import (
    ImageCapabilityProfile,
    ImageClassificationResult,
    ImageContentTag,
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
    )


def test_reserved_base_smoke_produces_narrow_capability_and_thumbnail_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, digest = _copy(tmp_path)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.delenv("HF_TOKEN", raising=False)

    class FakePipeline:
        def generate(self, resolved: object, progress: object) -> tuple[Image.Image, ...]:
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
    assert result.image_capability_profile.modes == ("txt2img",)
    assert result.image_capability_profile.max_width == 512
    assert result.image_capability_profile.max_outputs == 1


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


def test_auxiliary_asset_remains_unpublished_without_mode_specific_smoke(tmp_path: Path) -> None:
    store, digest = _copy(tmp_path)
    with pytest.raises(ImageValidationUnavailable, match="auxiliary execution validation"):
        validate_image_asset(
            store, _request(digest, kind=ModelKind.CONTROL_MODEL), reservation_bytes=10**9
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
