"""Image process launch persists identity before reporting a reservation."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from coire_core.models.image_worker import (
    ImageWorkerLoadRequest,
    ImageWorkerLoadResult,
    ImageWorkerProcessRecord,
)
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
    command: ClassVar[list[str]] = []
    alive = True
    created = 100.0

    def __init__(self, pid: int) -> None:
        assert pid == FakeProcess.pid

    def create_time(self) -> float:
        return self.created

    def cmdline(self) -> list[str]:
        return self.command

    def is_running(self) -> bool:
        return self.alive


def _setup(tmp_path: Path) -> tuple[Settings, Store, ImageWorkerLoadRequest]:
    FakePsutilProcess.command = []
    FakePsutilProcess.alive = True
    FakePsutilProcess.created = 100.0
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


async def test_authenticated_health_proves_ready_and_restart_adopts_exact_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, store, request = _setup(tmp_path)
    monkeypatch.setattr(
        "coire_node.image_runtime.supervisor.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    monkeypatch.setattr("coire_node.image_runtime.supervisor.psutil.Process", FakePsutilProcess)
    manager = supervisor.ImageProcessSupervisor(
        settings, store, lambda: 0, memory_total_bytes=10_000
    )
    starting = manager.start(request)
    assert starting.pid is not None
    record = ImageWorkerProcessRecord.model_validate_json(manager.record_path.read_bytes())
    FakePsutilProcess.command = [
        "python",
        "-m",
        "coire_node.image_runtime.bootstrap",
        str(record.config.token_file.parent / "launch.json"),
    ]
    FakePsutilProcess.alive = True
    FakePsutilProcess.created = 100.0
    token = record.config.token_file.read_text()

    def health(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {token}"
        assert request.url.host == "127.0.0.1" and request.url.port == 9600
        return httpx.Response(
            200,
            json=ImageWorkerLoadResult(
                instance_id=starting.instance_id,
                state="ready",
                pid=starting.pid,
                process_create_time=starting.process_create_time,
                port=starting.port,
                reserved_bytes=starting.reserved_bytes,
            ).model_dump(mode="json"),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(health)) as client:
        ready = await manager.refresh_ready(client)
    assert ready.state == "ready"
    assert (
        ImageWorkerProcessRecord.model_validate_json(manager.record_path.read_bytes()).status
        == ready
    )
    restarted = supervisor.ImageProcessSupervisor(
        settings, store, lambda: 0, memory_total_bytes=10_000
    )
    assert restarted.committed_bytes() == 9000
    assert restarted.adopt_from_state() == ready
    assert restarted.committed_bytes() == 1000


async def test_forged_health_or_changed_process_never_marks_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, store, request = _setup(tmp_path)
    monkeypatch.setattr(
        "coire_node.image_runtime.supervisor.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    monkeypatch.setattr("coire_node.image_runtime.supervisor.psutil.Process", FakePsutilProcess)
    manager = supervisor.ImageProcessSupervisor(
        settings, store, lambda: 0, memory_total_bytes=10_000
    )
    starting = manager.start(request)
    assert starting.pid is not None
    record = ImageWorkerProcessRecord.model_validate_json(manager.record_path.read_bytes())
    FakePsutilProcess.command = [
        "python",
        "-m",
        "coire_node.image_runtime.bootstrap",
        str(record.config.token_file.parent / "launch.json"),
    ]
    FakePsutilProcess.alive = True
    FakePsutilProcess.created = 100.0
    forged = starting.model_copy(update={"state": "ready", "pid": starting.pid + 1})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json=forged.model_dump(mode="json"))
        )
    ) as client:
        assert (await manager.refresh_ready(client)).state == "starting"
    assert manager.committed_bytes() == 1000
    valid = starting.model_copy(update={"state": "ready"})

    def swapped_after_request(req: httpx.Request) -> httpx.Response:
        FakePsutilProcess.created = 101.0
        return httpx.Response(200, json=valid.model_dump(mode="json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(swapped_after_request)) as client:
        assert (await manager.refresh_ready(client)).state == "starting"
    FakePsutilProcess.created = 101.0
    assert manager.adopt_from_state() == starting  # already held by this manager
    restarted = supervisor.ImageProcessSupervisor(
        settings, store, lambda: 0, memory_total_bytes=10_000
    )
    with pytest.raises(supervisor.ImageProcessUnavailable):
        restarted.adopt_from_state()
    assert restarted.committed_bytes() == 9000


def test_tampered_private_record_refuses_adoption_and_keeps_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, store, request = _setup(tmp_path)
    monkeypatch.setattr(
        "coire_node.image_runtime.supervisor.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    monkeypatch.setattr("coire_node.image_runtime.supervisor.psutil.Process", FakePsutilProcess)
    manager = supervisor.ImageProcessSupervisor(
        settings, store, lambda: 0, memory_total_bytes=10_000
    )
    manager.start(request)
    manager.record_path.chmod(0o644)
    restarted = supervisor.ImageProcessSupervisor(
        settings, store, lambda: 0, memory_total_bytes=10_000
    )
    with pytest.raises(supervisor.ImageProcessUnavailable):
        restarted.adopt_from_state()
    assert restarted.record_path.exists()
    assert restarted.committed_bytes() == 9000
