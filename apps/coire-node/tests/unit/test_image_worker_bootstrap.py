"""The native image child sets offline mode before importing its engine."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from coire_core.models.image_worker import ImageWorkerLoadRequest, ImageWorkerProcessConfig
from coire_node.image_runtime import bootstrap


def _files(tmp_path: Path) -> tuple[Path, ImageWorkerProcessConfig]:
    load = ImageWorkerLoadRequest(
        slug="studio--z-image-turbo",
        model_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        manifest_sha256="a" * 64,
        reservation_bytes=1024,
        runtime_version="mflux-0.20.0",
    )
    config = ImageWorkerProcessConfig(
        load=load,
        store_dir=tmp_path / "store",
        scratch_dir=tmp_path / "scratch",
        token_file=tmp_path / "secret",
        port=39177,
    )
    config.token_file.write_text("t" * 64)
    config.token_file.chmod(0o600)
    path = tmp_path / "worker.json"
    path.write_text(config.model_dump_json())
    path.chmod(0o600)
    return path, config


@pytest.mark.parametrize(
    "change",
    ["config_mode", "token_mode", "config_symlink", "token_symlink", "weak_token", "large_config"],
)
def test_launch_rejects_untrusted_files_before_import(tmp_path: Path, change: str) -> None:
    path, config = _files(tmp_path)
    if change == "config_mode":
        path.chmod(0o644)
    elif change == "token_mode":
        config.token_file.chmod(0o644)
    elif change == "config_symlink":
        linked = tmp_path / "link.json"
        linked.symlink_to(path)
        path = linked
    elif change == "token_symlink":
        linked = tmp_path / "link-token"
        linked.symlink_to(config.token_file)
        path.write_text(config.model_copy(update={"token_file": linked}).model_dump_json())
    elif change == "weak_token":
        config.token_file.write_text("weak")
    elif change == "large_config":
        path.write_bytes(b"x" * (16 * 1024 + 1))
    with pytest.raises(bootstrap.ImageWorkerBootstrapError):
        bootstrap.read_process_config(path)


async def test_bootstrap_strips_credentials_and_imports_native_modules_lazily(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, config = _files(tmp_path)
    monkeypatch.setenv("HF_TOKEN", "credential")
    monkeypatch.setenv("HUGGING_FACE_HUB_TOKEN", "credential")
    called: list[str] = []

    class FakeStore:
        def __init__(self, root: Path) -> None:
            assert root == config.store_dir
            called.append("store")

    class FakePipeline:
        @classmethod
        def load(cls, store: FakeStore, request: ImageWorkerLoadRequest) -> object:
            assert request == config.load
            assert os.environ["HF_HUB_OFFLINE"] == "1"
            assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
            assert "HF_TOKEN" not in os.environ
            assert "HUGGING_FACE_HUB_TOKEN" not in os.environ
            called.append("load")
            return object()

    def fake_app(
        load: ImageWorkerLoadRequest, pipeline: object, scratch: Path, *, token: str, port: int
    ) -> object:
        assert load == config.load and scratch == config.scratch_dir
        assert token == "t" * 64 and port == config.port
        called.append("app")
        return object()

    async def fake_serve(app: object, *, port: int) -> None:
        assert port == config.port
        called.append("serve")

    def fake_import(name: str) -> object:
        called.append(f"import:{name}")
        assert os.environ["HF_HUB_OFFLINE"] == "1"
        assert "HF_TOKEN" not in os.environ
        return {
            "coire_node.store": SimpleNamespace(Store=FakeStore),
            "coire_node.image_runtime.pipeline": SimpleNamespace(MfluxTxt2ImgPipeline=FakePipeline),
            "coire_node.image_runtime.control": SimpleNamespace(
                create_worker_app=fake_app, serve_worker=fake_serve
            ),
        }[name]

    monkeypatch.setattr("coire_node.image_runtime.bootstrap.importlib.import_module", fake_import)
    await bootstrap.run_process(path)
    assert called == [
        "import:coire_node.store",
        "import:coire_node.image_runtime.pipeline",
        "import:coire_node.image_runtime.control",
        "store",
        "load",
        "app",
        "serve",
    ]
