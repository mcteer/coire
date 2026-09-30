"""Image process launch persists identity before reporting a reservation."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from coire_core.models.image_worker import ImageWorkerLoadRequest, ImageWorkerProcessRecord
from coire_core.settings import Settings
from coire_node.image_runtime import supervisor
from coire_node.store import Store

SLUG = "studio--z-image-turbo"


class FakeProcess:
    pid = 123456789

    def __init__(self) -> None:
        self.killed = False

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        return -9 if self.killed else 0


class FakePsutilProcess:
    def __init__(self, pid: int) -> None:
        assert pid == FakeProcess.pid

    def create_time(self) -> float:
        return 100.0


def _setup(tmp_path: Path) -> tuple[Settings, Store, ImageWorkerLoadRequest]:
    settings = Settings(
        _secrets_dir="/nonexistent",  # type: ignore[call-arg]
        node_store_dir=str(tmp_path / "models"),
        node_state_dir=str(tmp_path / "state"),
        node_image_worker_port=9600,
        node_memory_budget_fraction=0.9,
    )
    store = Store(settings.node_store_dir)
    root = store.path_for(SLUG)
    root.mkdir(parents=True)
    (root / "config.json").write_text("{}")
    (root / "model.safetensors").write_bytes(b"safe")
    manifest = store.hash_tree(SLUG, repo_id="studio/z-image-turbo", revision="v1")
    store.write_manifest(manifest)
    request = ImageWorkerLoadRequest(
        slug=SLUG,
        model_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        manifest_sha256=manifest.sha256(),
        reservation_bytes=1000,
        runtime_version="mflux-0.20.0",
    )
    return settings, store, request


def test_launch_is_offline_private_and_durable_before_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, store, request = _setup(tmp_path)
    process = FakeProcess()
    captured: dict[str, Any] = {}

    def fake_spawn(argv: list[str], **kwargs: object) -> FakeProcess:
        captured["argv"] = argv
        captured.update(kwargs)
        return process

    monkeypatch.setenv("HF_TOKEN", "secret")
    monkeypatch.setenv("HUGGING_FACE_HUB_TOKEN", "secret")
    monkeypatch.setattr("coire_node.image_runtime.supervisor.subprocess.Popen", fake_spawn)
    monkeypatch.setattr("coire_node.image_runtime.supervisor.psutil.Process", FakePsutilProcess)
    manager = supervisor.ImageProcessSupervisor(
        settings, store, lambda: 500, memory_total_bytes=10_000
    )
    status = manager.start(request)
    assert status.state == "starting"
    assert status.pid == process.pid and status.process_create_time == 100.0
    assert status.port == 9600 and status.reserved_bytes == 1000
    assert manager.committed_bytes() == 1000
    assert captured["start_new_session"] is True
    argv = captured["argv"]
    assert isinstance(argv, list) and argv[1:3] == ["-m", "coire_node.image_runtime.bootstrap"]
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["HF_HUB_OFFLINE"] == "1" and env["TRANSFORMERS_OFFLINE"] == "1"
    assert "HF_TOKEN" not in env and "HUGGING_FACE_HUB_TOKEN" not in env
    record = ImageWorkerProcessRecord.model_validate_json(manager.record_path.read_bytes())
    assert record.status == status
    assert record.config.load == request
    assert record.config.token_file.stat().st_mode & 0o777 == 0o600
    assert record.config.token_file.parent.stat().st_mode & 0o777 == 0o700
    assert (record.config.token_file.parent / "launch.json").stat().st_mode & 0o777 == 0o600
    assert record.config.token_file.read_text() not in manager.record_path.read_text()
    assert manager.record_path.stat().st_mode & 0o777 == 0o600
    assert manager.start(request) == status
    with pytest.raises(supervisor.ImageProcessUnavailable):
        manager.start(request.model_copy(update={"instance_id": uuid.uuid4()}))
    assert not process.killed


def test_budget_and_bad_copy_refuse_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, store, request = _setup(tmp_path)
    monkeypatch.setattr(
        "coire_node.image_runtime.supervisor.subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("spawned"),
    )
    over = supervisor.ImageProcessSupervisor(
        settings, store, lambda: 8500, memory_total_bytes=10_000
    )
    with pytest.raises(supervisor.ImageProcessUnavailable):
        over.start(request)
    assert not over.record_path.exists()
    affordable = supervisor.ImageProcessSupervisor(
        settings, store, lambda: 0, memory_total_bytes=10_000
    )
    with pytest.raises(supervisor.ImageProcessUnavailable):
        affordable.start(request.model_copy(update={"manifest_sha256": "0" * 64}))
    assert not affordable.record_path.exists()


def test_persist_failure_kills_child_and_releases_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, store, request = _setup(tmp_path)
    process = FakeProcess()
    monkeypatch.setattr(
        "coire_node.image_runtime.supervisor.subprocess.Popen", lambda *args, **kwargs: process
    )
    monkeypatch.setattr("coire_node.image_runtime.supervisor.psutil.Process", FakePsutilProcess)
    monkeypatch.setattr(
        supervisor, "write_atomic", lambda path, data: (_ for _ in ()).throw(OSError("disk"))
    )
    manager = supervisor.ImageProcessSupervisor(
        settings, store, lambda: 0, memory_total_bytes=10_000
    )
    with pytest.raises(supervisor.ImageProcessUnavailable):
        manager.start(request)
    assert process.killed
    assert manager.committed_bytes() == 0
    assert not manager.record_path.exists()
