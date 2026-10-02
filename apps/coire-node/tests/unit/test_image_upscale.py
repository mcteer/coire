"""Pinned upscale stage uses only a verified local SeedVR2 copy."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from coire_core.image_assets import SEEDVR2_3B_FILES, SEEDVR2_3B_REPO_ID
from coire_core.models.images import ImageManifestDigest
from coire_node.image_runtime import upscale
from coire_node.store import Store


def _dependency() -> ImageManifestDigest:
    return ImageManifestDigest(
        model_id=uuid.uuid4(),
        slug="numz--seedvr2-comfyui",
        revision="a" * 40,
        sha256="b" * 64,
    )


def test_local_upscale_preserves_exact_factor_and_rejects_changed_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    dependency = _dependency()
    store = Store(tmp_path)
    manifest = SimpleNamespace(
        revision=dependency.revision,
        repo_id=SEEDVR2_3B_REPO_ID,
        files=[SimpleNamespace(path=name) for name in SEEDVR2_3B_FILES],
    )
    monkeypatch.setattr(store, "read_manifest", lambda slug: manifest)
    checked: list[object] = []

    def verify(_store: Store, load: object) -> Path:
        checked.append(load)
        return tmp_path

    monkeypatch.setattr(upscale, "verify_image_copy", verify)
    monkeypatch.setattr(upscale, "resident_bytes", lambda _pid: 512)

    class Native:
        def __init__(self, *, model_path: str, model_config: object) -> None:
            assert model_path == str(tmp_path) and model_config == "seedvr2"

        def parameters(self) -> object:
            return object()

        def generate_image(self, *, seed: int, image_path: Path, resolution: int) -> object:
            assert seed == 7 and resolution == 128
            with Image.open(image_path) as source:
                assert source.size == (64, 96)
            return SimpleNamespace(image=Image.new("RGB", (128, 192)))

    def import_module(name: str) -> Any:
        if name.endswith("seedvr2"):
            return SimpleNamespace(SeedVR2=Native)
        if name.endswith("model_config"):
            return SimpleNamespace(ModelConfig=SimpleNamespace(seedvr2_3b=lambda: "seedvr2"))
        assert name == "mlx.core"
        return SimpleNamespace(eval=lambda _value: None)

    monkeypatch.setattr("coire_node.image_runtime.upscale.importlib.import_module", import_module)
    with Image.new("RGB", (64, 96)) as source:
        image = upscale.upscale_image(
            source,
            store=store,
            dependency=dependency,
            instance_id=uuid.uuid4(),
            reservation_bytes=1024,
            runtime_version="mflux-0.20.0",
            seed=7,
            factor=2,
        )
    assert image.size == (128, 192)
    image.close()
    assert len(checked) == 1
    assert list(tmp_path.glob("coire-upscale-*")) == []
    manifest.repo_id = "unreviewed/model"
    with Image.new("RGB", (64, 96)) as source, pytest.raises(upscale.ImageUpscaleUnavailable):
        upscale.upscale_image(
            source,
            store=store,
            dependency=dependency,
            instance_id=uuid.uuid4(),
            reservation_bytes=1024,
            runtime_version="mflux-0.20.0",
            seed=7,
            factor=2,
        )
    manifest.repo_id = SEEDVR2_3B_REPO_ID
    memory = {"bytes": 512}
    monkeypatch.setattr(upscale, "resident_bytes", lambda _pid: memory["bytes"])
    original_generate = Native.generate_image

    def over_budget(self: Native, *, seed: int, image_path: Path, resolution: int) -> object:
        generated = original_generate(self, seed=seed, image_path=image_path, resolution=resolution)
        memory["bytes"] = 2048
        return generated

    monkeypatch.setattr(Native, "generate_image", over_budget)
    with Image.new("RGB", (64, 96)) as source, pytest.raises(upscale.ImageUpscaleUnavailable):
        upscale.upscale_image(
            source,
            store=store,
            dependency=dependency,
            instance_id=uuid.uuid4(),
            reservation_bytes=1024,
            runtime_version="mflux-0.20.0",
            seed=7,
            factor=2,
        )
    monkeypatch.setattr(upscale, "resident_bytes", lambda _pid: 2048)
    with Image.new("RGB", (64, 96)) as source, pytest.raises(upscale.ImageUpscaleUnavailable):
        upscale.upscale_image(
            source,
            store=store,
            dependency=dependency,
            instance_id=uuid.uuid4(),
            reservation_bytes=1024,
            runtime_version="mflux-0.20.0",
            seed=7,
            factor=2,
        )
