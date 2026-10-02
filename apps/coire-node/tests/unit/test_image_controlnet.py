"""Canny jobs use the pinned local Union asset and requested thresholds."""

from __future__ import annotations

import importlib
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from coire_core.image_assets import CONTROL_UNION_FILE, CONTROL_UNION_REPO_ID
from coire_core.models.image_worker import ImageWorkerLoadRequest
from coire_core.models.images import ImageControl, ImageManifestDigest
from coire_node.image_runtime import controlnet
from coire_node.image_runtime.cache import StageCache, StageCacheKey, stage_identity
from coire_node.store import Store


def test_local_union_control_uses_exact_canny_thresholds_and_removes_composite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    base_id, control_id = uuid.uuid4(), uuid.uuid4()
    base = ImageWorkerLoadRequest(
        slug="studio--z-image-turbo",
        model_id=base_id,
        instance_id=uuid.uuid4(),
        manifest_sha256="a" * 64,
        reservation_bytes=1024,
        runtime_version="mflux-0.20.0",
    )
    dependency = ImageManifestDigest(
        model_id=control_id,
        slug="alibaba--union-control",
        revision="b" * 40,
        sha256="c" * 64,
    )
    control = ImageControl(
        image_id=uuid.uuid4(),
        model_id=control_id,
        strength=Decimal("0.375125"),
        low_threshold=23,
        high_threshold=89,
    )
    base_path, control_path = tmp_path / "base", tmp_path / "control"
    base_path.mkdir()
    control_path.mkdir()
    (base_path / "weights.safetensors").write_bytes(b"base")
    (control_path / CONTROL_UNION_FILE).write_bytes(b"control")
    manifest = SimpleNamespace(
        revision=dependency.revision,
        repo_id=CONTROL_UNION_REPO_ID,
        files=[SimpleNamespace(path=CONTROL_UNION_FILE)],
    )
    store = Store(tmp_path)
    monkeypatch.setattr(
        store,
        "read_manifest",
        lambda slug: (
            SimpleNamespace(files=[SimpleNamespace(path="weights.safetensors")])
            if slug == base.slug
            else manifest
        ),
    )
    monkeypatch.setattr(
        controlnet,
        "verify_image_copy",
        lambda _store, load: base_path if load.slug == base.slug else control_path,
    )
    monkeypatch.setattr(controlnet, "resident_bytes", lambda _pid: 512)
    thresholds: list[tuple[int, int]] = []

    def canny(image: Any, low: int, high: int) -> Any:
        assert image is not None
        thresholds.append((low, high))
        return object()

    monkeypatch.setattr(Image, "fromarray", lambda _array: Image.new("L", (64, 64), "white"))
    preprocess = SimpleNamespace(ZImageControlnetUtil=SimpleNamespace(_preprocess=lambda *_: None))
    callback = object()

    class Native:
        def __init__(self, *, model_path: str, model_config: object) -> None:
            assert model_config == "union"
            composite = Path(model_path)
            assert (composite / "weights.safetensors").read_bytes() == b"base"
            assert (composite / "controlnet" / CONTROL_UNION_FILE).read_bytes() == b"control"
            self.callbacks = SimpleNamespace(register=lambda actual: assert_callback(actual))

        def parameters(self) -> object:
            return object()

        def generate_image(self, **kwargs: object) -> Image.Image:
            assert kwargs["controlnet_strength"] == float(control.strength)
            controls = kwargs["controls"]
            assert isinstance(controls, list)
            edge_path = controls[0]["image_path"]
            with Image.open(edge_path) as edges:
                assert edges.size == (64, 64)
                assert preprocess.ZImageControlnetUtil._preprocess(edges, "canny") is edges
            return Image.new("RGB", (64, 64))

    def assert_callback(actual: object) -> None:
        assert actual is callback

    actual_import = importlib.import_module

    def fake_import(name: str) -> Any:
        if name.endswith("z_image_turbo_controlnet"):
            return SimpleNamespace(ZImageTurboControlnet=Native)
        if name.endswith("control_types"):
            return SimpleNamespace(
                ControlType=SimpleNamespace(canny="canny"),
                ControlSpec=lambda **kwargs: kwargs,
            )
        if name.endswith("controlnet_util"):
            return preprocess
        if name.endswith("model_config"):
            return SimpleNamespace(
                ModelConfig=SimpleNamespace(z_image_turbo_controlnet_union_2_1=lambda: "union")
            )
        if name == "mlx.core":
            return SimpleNamespace(eval=lambda *_: None, clear_cache=lambda: None)
        if name == "cv2":
            return SimpleNamespace(Canny=canny)
        if name == "numpy":
            return SimpleNamespace(asarray=lambda image: image)
        return actual_import(name)

    monkeypatch.setattr("coire_node.image_runtime.controlnet.importlib.import_module", fake_import)
    source = tmp_path / "source.png"
    with Image.new("RGB", (64, 64), "white") as image:
        image.save(source)
    edge_cache = StageCache(1024 * 1024)
    key = StageCacheKey(
        stage="control", owner_id=str(control.image_id), identity=stage_identity("edges")
    )
    for _ in range(2):
        with controlnet.LocalCannyControlStage(
            store=store,
            base=base,
            dependency=dependency,
            control=control,
            source=source,
            width=64,
            height=64,
            callback=callback,
            edge_cache=edge_cache,
            edge_key=key,
        ) as stage:
            output = stage.generate(seed=7, prompt="private subject", steps=2)
        assert output.size == (64, 64)
        output.close()
    assert thresholds == [(23, 89)]
    assert edge_cache.occupancy("control") > 0
    assert list(tmp_path.glob("coire-control-*")) == []
