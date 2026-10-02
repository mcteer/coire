"""Exact local SeedVR2 stage for an already generated RGB image."""

from __future__ import annotations

import importlib
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import cast

from PIL import Image

from coire_core.image_assets import SEEDVR2_3B_FILES, SEEDVR2_3B_REPO_ID
from coire_core.models.image_worker import ImageWorkerLoadRequest
from coire_core.models.images import ImageManifestDigest
from coire_node.footprint import resident_bytes
from coire_node.image_runtime.preflight import verify_image_copy
from coire_node.store import Store


class ImageUpscaleUnavailable(RuntimeError):
    """The requested verified local upscale stage cannot run."""


def upscale_image(
    source: Image.Image,
    *,
    store: Store,
    dependency: ImageManifestDigest,
    instance_id: uuid.UUID,
    reservation_bytes: int,
    runtime_version: str,
    seed: int,
    factor: int,
) -> Image.Image:
    """Load the pinned standalone asset and return exactly factor-scaled pixels."""
    if (
        os.environ.get("HF_HUB_OFFLINE") != "1"
        or os.environ.get("TRANSFORMERS_OFFLINE") != "1"
        or os.environ.get("HF_TOKEN")
        or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    ):
        raise ImageUpscaleUnavailable()
    if dependency.slug is None or dependency.variant_id is not None or factor not in {2, 4}:
        raise ImageUpscaleUnavailable()
    load = ImageWorkerLoadRequest(
        slug=dependency.slug,
        model_id=dependency.model_id,
        instance_id=instance_id,
        manifest_sha256=dependency.sha256,
        reservation_bytes=reservation_bytes,
        runtime_version=runtime_version,
    )
    path = verify_image_copy(store, load)
    manifest = store.read_manifest(dependency.slug)
    if (
        manifest is None
        or manifest.revision != dependency.revision
        or manifest.repo_id != SEEDVR2_3B_REPO_ID
        or {entry.path for entry in manifest.files if entry.path.endswith(".safetensors")}
        != SEEDVR2_3B_FILES
    ):
        raise ImageUpscaleUnavailable()
    first = resident_bytes(os.getpid())
    if first is None or first > reservation_bytes:
        raise ImageUpscaleUnavailable()
    peak = first
    unavailable = False
    stop = threading.Event()

    def sample() -> None:
        nonlocal peak, unavailable
        while not stop.wait(0.01):
            current = resident_bytes(os.getpid())
            if current is None:
                unavailable = True
                return
            peak = max(peak, current)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        module = importlib.import_module("mflux.models.seedvr2.variants.upscale.seedvr2")
        config_module = importlib.import_module("mflux.models.common.config.model_config")
        model = module.SeedVR2(
            model_path=str(path), model_config=config_module.ModelConfig.seedvr2_3b()
        )
        mlx = importlib.import_module("mlx.core")
        mlx.eval(model.parameters())
        with tempfile.TemporaryDirectory(prefix="coire-upscale-", dir=store.root) as directory:
            source_path = Path(directory) / "source.png"
            source.save(source_path, format="PNG")
            result = model.generate_image(
                seed=seed,
                image_path=source_path,
                resolution=min(source.size) * factor,
            )
    finally:
        stop.set()
        sampler.join(timeout=2)
    current = resident_bytes(os.getpid())
    if (
        current is None
        or unavailable
        or sampler.is_alive()
        or max(peak, current) > reservation_bytes
    ):
        image = getattr(result, "image", None)
        if isinstance(image, Image.Image):
            image.close()
        raise ImageUpscaleUnavailable()
    image = cast(Image.Image | None, getattr(result, "image", None))
    if (
        image is None
        or image.mode != "RGB"
        or image.size
        != (
            source.width * factor,
            source.height * factor,
        )
    ):
        if image is not None:
            image.close()
        raise ImageUpscaleUnavailable()
    return image
