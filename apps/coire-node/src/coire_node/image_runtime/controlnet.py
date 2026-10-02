"""Pinned local Union control stage with exact Canny preprocessing."""

from __future__ import annotations

import gc
import importlib
import os
import tempfile
import threading
from pathlib import Path
from types import ModuleType, TracebackType
from unittest.mock import patch

from PIL import Image

from coire_core.image_assets import CONTROL_UNION_FILE, CONTROL_UNION_REPO_ID
from coire_core.models.image_worker import ImageWorkerLoadRequest
from coire_core.models.images import ImageControl, ImageManifestDigest
from coire_node.footprint import resident_bytes
from coire_node.image_runtime.preflight import verify_image_copy
from coire_node.store import Store


class ImageControlUnavailable(RuntimeError):
    """The exact local control composite cannot be used."""


class LocalCannyControlStage:
    """One serial control model, bounded by the whole worker memory hold."""

    def __init__(
        self,
        *,
        store: Store,
        base: ImageWorkerLoadRequest,
        dependency: ImageManifestDigest,
        control: ImageControl,
        source: Path,
        width: int,
        height: int,
        callback: object,
    ) -> None:
        self.store = store
        self.base = base
        self.dependency = dependency
        self.control = control
        self.source = source
        self.width = width
        self.height = height
        self.callback = callback
        self._directory: tempfile.TemporaryDirectory[str] | None = None
        self._model: object | None = None
        self._edge_path: Path | None = None
        self._types: ModuleType | None = None
        self._util: ModuleType | None = None
        self._stop = threading.Event()
        self._sampler: threading.Thread | None = None
        self._peak = 0
        self._unavailable = False

    def _sample(self) -> None:
        while not self._stop.wait(0.01):
            current = resident_bytes(os.getpid())
            if current is None:
                self._unavailable = True
                return
            self._peak = max(self._peak, current)

    def __enter__(self) -> LocalCannyControlStage:
        if (
            os.environ.get("HF_HUB_OFFLINE") != "1"
            or os.environ.get("TRANSFORMERS_OFFLINE") != "1"
            or os.environ.get("HF_TOKEN")
            or os.environ.get("HUGGING_FACE_HUB_TOKEN")
            or self.control.type != "canny"
            or self.control.variant_id is not None
            or self.control.model_id != self.dependency.model_id
            or self.dependency.variant_id is not None
            or self.dependency.slug is None
        ):
            raise ImageControlUnavailable()
        base_path = verify_image_copy(self.store, self.base)
        base_manifest = self.store.read_manifest(self.base.slug)
        auxiliary = ImageWorkerLoadRequest(
            slug=self.dependency.slug,
            model_id=self.dependency.model_id,
            instance_id=self.base.instance_id,
            manifest_sha256=self.dependency.sha256,
            reservation_bytes=self.base.reservation_bytes,
            runtime_version=self.base.runtime_version,
        )
        auxiliary_path = verify_image_copy(self.store, auxiliary)
        manifest = self.store.read_manifest(self.dependency.slug)
        if (
            base_manifest is None
            or manifest is None
            or manifest.revision != self.dependency.revision
            or manifest.repo_id != CONTROL_UNION_REPO_ID
            or {entry.path for entry in manifest.files if entry.path.endswith(".safetensors")}
            != {CONTROL_UNION_FILE}
        ):
            raise ImageControlUnavailable()
        first = resident_bytes(os.getpid())
        if first is None or first > self.base.reservation_bytes:
            raise ImageControlUnavailable()
        self._peak = first
        self._directory = tempfile.TemporaryDirectory(prefix="coire-control-", dir=self.store.root)
        self._sampler = threading.Thread(target=self._sample, daemon=True)
        self._sampler.start()
        try:
            composite = Path(self._directory.name)
            for entry in base_manifest.files:
                target = composite / entry.path
                target.parent.mkdir(parents=True, exist_ok=True)
                os.link(base_path / entry.path, target, follow_symlinks=False)
            control_root = composite / "controlnet"
            control_root.mkdir(mode=0o700)
            os.link(auxiliary_path / CONTROL_UNION_FILE, control_root / CONTROL_UNION_FILE)
            with Image.open(self.source) as raw:
                if raw.size != (self.width, self.height):
                    raise ImageControlUnavailable()
                gray = raw.convert("L")
                cv2 = importlib.import_module("cv2")
                np = importlib.import_module("numpy")
                edges = cv2.Canny(
                    np.asarray(gray), self.control.low_threshold, self.control.high_threshold
                )
                self._edge_path = composite / "canny.png"
                Image.fromarray(edges).convert("RGB").save(self._edge_path, format="PNG")
            module = importlib.import_module(
                "mflux.models.z_image.variants.controlnet.z_image_turbo_controlnet"
            )
            self._types = importlib.import_module(
                "mflux.models.z_image.variants.controlnet.control_types"
            )
            self._util = importlib.import_module(
                "mflux.models.z_image.variants.controlnet.controlnet_util"
            )
            config_module = importlib.import_module("mflux.models.common.config.model_config")
            model = module.ZImageTurboControlnet(
                model_path=str(composite),
                model_config=config_module.ModelConfig.z_image_turbo_controlnet_union_2_1(),
            )
            mlx = importlib.import_module("mlx.core")
            mlx.eval(model.parameters())
            model.callbacks.register(self.callback)
            self._model = model
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def generate(self, *, seed: int, prompt: str, steps: int) -> Image.Image:
        if (
            self._model is None
            or self._types is None
            or self._util is None
            or self._edge_path is None
        ):
            raise ImageControlUnavailable()
        control_spec = self._types.ControlSpec(
            type=self._types.ControlType.canny, image_path=self._edge_path
        )
        with patch.object(
            self._util.ZImageControlnetUtil,
            "_preprocess",
            staticmethod(lambda image, _control_type: image),
        ):
            generated = self._model.generate_image(  # type: ignore[attr-defined]
                seed=seed,
                prompt=prompt,
                controls=[control_spec],
                num_inference_steps=steps,
                height=self.height,
                width=self.width,
                controlnet_strength=float(self.control.strength),
            )
        image = (
            generated if isinstance(generated, Image.Image) else getattr(generated, "image", None)
        )
        if (
            not isinstance(image, Image.Image)
            or image.mode != "RGB"
            or image.size
            != (
                self.width,
                self.height,
            )
        ):
            if isinstance(image, Image.Image):
                image.close()
            raise ImageControlUnavailable()
        return image

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_value, traceback
        self._stop.set()
        if self._sampler is not None:
            self._sampler.join(timeout=2)
        self._model = None
        gc.collect()
        try:
            mlx = importlib.import_module("mlx.core")
            mlx.clear_cache()
        finally:
            if self._directory is not None:
                self._directory.cleanup()
        current = resident_bytes(os.getpid())
        if exc_type is None and (
            current is None
            or self._unavailable
            or (self._sampler is not None and self._sampler.is_alive())
            or max(self._peak, current) > self.base.reservation_bytes
        ):
            raise ImageControlUnavailable()
