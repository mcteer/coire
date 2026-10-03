"""Credential-free offline entrypoint for a node-owned Studio image child."""

from __future__ import annotations

import asyncio
import importlib
import os
import re
import stat
import sys
from pathlib import Path

from coire_core.models.image_worker import ImageWorkerProcessConfig

_CONFIG_MAX_BYTES = 16 * 1024
_TOKEN_MAX_BYTES = 512
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


class ImageWorkerBootstrapError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("image worker bootstrap unavailable")


def _read_private(path: Path, limit: int) -> bytes:
    if not path.is_absolute() or ".." in path.parts:
        raise ImageWorkerBootstrapError()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()
                or not 0 < info.st_size <= limit
            ):
                raise ImageWorkerBootstrapError()
            data = os.read(fd, limit + 1)
            if len(data) != info.st_size:
                raise ImageWorkerBootstrapError()
            return data
        finally:
            os.close(fd)
    except OSError as exc:
        raise ImageWorkerBootstrapError() from exc


def read_process_config(path: Path) -> tuple[ImageWorkerProcessConfig, str]:
    """Accept only node-owned 0600 launch and token files."""
    try:
        config = ImageWorkerProcessConfig.model_validate_json(
            _read_private(path, _CONFIG_MAX_BYTES)
        )
        token = _read_private(config.token_file, _TOKEN_MAX_BYTES).decode("ascii")
        if _TOKEN_PATTERN.fullmatch(token) is None:
            raise ImageWorkerBootstrapError()
        return config, token
    except (UnicodeError, ValueError) as exc:
        raise ImageWorkerBootstrapError() from exc


def _offline_environment() -> None:
    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_HUB_TOKEN"):
        os.environ.pop(name, None)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["PYTHONNOUSERSITE"] = "1"


async def run_process(path: Path) -> None:
    config, token = read_process_config(path)
    _offline_environment()
    store_module = importlib.import_module("coire_node.store")
    pipeline_module = importlib.import_module("coire_node.image_runtime.pipeline")
    control_module = importlib.import_module("coire_node.image_runtime.control")
    store = store_module.Store(config.store_dir)
    pipeline = pipeline_module.MfluxTxt2ImgPipeline.load(
        store, config.load, prompt_cache_max_bytes=config.prompt_cache_max_bytes
    )
    classification_module = importlib.import_module("coire_node.image_runtime.classification")
    classifier_model_dir = classification_module.verified_classifier_copy(store)
    app = control_module.create_worker_app(
        config.load,
        pipeline,
        config.scratch_dir,
        token=token,
        port=config.port,
        classifier_model_dir=classifier_model_dir,
        classifier_memory_bytes=config.classifier_memory_bytes,
    )
    await control_module.serve_worker(app, port=config.port)


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    try:
        asyncio.run(run_process(Path(sys.argv[1])))
    except Exception:
        print("image worker unavailable", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
