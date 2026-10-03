"""Image process launch persists identity before reporting a reservation."""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import httpx
import psutil
import pytest

from coire_core.models.image_worker import (
    ImageWorkerLoadRequest,
    ImageWorkerLoadResult,
    ImageWorkerProcessRecord,
    ImageWorkerUnloadRequest,
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
    inspection_error = False

    def __init__(self, pid: int) -> None:
        assert pid == FakeProcess.pid
        self.pid = pid

    def create_time(self) -> float:
        return self.created

    def cmdline(self) -> list[str]:
        if self.inspection_error:
            raise psutil.AccessDenied(self.pid)
        return self.command

    def is_running(self) -> bool:
        return self.alive

    def status(self) -> str:
        return "running" if self.alive else psutil.STATUS_ZOMBIE


def _setup(tmp_path: Path) -> tuple[Settings, Store, ImageWorkerLoadRequest]:
    FakePsutilProcess.command = []
    FakePsutilProcess.alive = True
    FakePsutilProcess.created = 100.0
    FakePsutilProcess.inspection_error = False
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
    settings.image_prompt_cache_max_bytes = 4096
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
    assert record.config.prompt_cache_max_bytes == 4096
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
    adopted = restarted.adopt_from_state()
    assert adopted is not None
    assert adopted == ready.model_copy(update={"state": "starting"})
    assert restarted.committed_bytes() == 1000
    async with httpx.AsyncClient(transport=httpx.MockTransport(health)) as client:
        assert await restarted.refresh_ready(client) == ready


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


def _started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[supervisor.ImageProcessSupervisor, ImageWorkerLoadRequest]:
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
    record = ImageWorkerProcessRecord.model_validate_json(manager.record_path.read_bytes())
    FakePsutilProcess.command = [
        "python",
        "-m",
        "coire_node.image_runtime.bootstrap",
        str(record.config.token_file.parent / "launch.json"),
    ]
    return manager, request


def _unload(request: ImageWorkerLoadRequest) -> ImageWorkerUnloadRequest:
    return ImageWorkerUnloadRequest(
        instance_id=request.instance_id, reason="admin", requested_at=datetime.now(UTC)
    )


def test_stop_term_confirms_death_before_releasing_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, request = _started(tmp_path, monkeypatch)
    signals: list[int] = []

    def fake_signal(pid: int, sig: int) -> None:
        assert pid == FakeProcess.pid
        signals.append(sig)
        FakePsutilProcess.alive = False

    monkeypatch.setattr("coire_node.image_runtime.supervisor.os.killpg", fake_signal)
    scratch = manager.scratch_root
    scratch.mkdir(mode=0o700)
    (scratch / "retained.png").write_bytes(b"private")
    stopped = manager.stop(_unload(request))
    assert stopped.reserved_bytes == 0 and stopped.safe_error == "worker_stopped"
    assert signals == [15]
    assert manager.committed_bytes() == 0
    assert not manager.record_path.exists()
    assert (scratch / "retained.png").read_bytes() == b"private"
    assert manager.stop(_unload(request)) == stopped
    restarted = supervisor.ImageProcessSupervisor(
        manager.settings, manager.store, lambda: 0, memory_total_bytes=10_000
    )
    assert restarted.stop(_unload(request)) == stopped
    with pytest.raises(supervisor.ImageProcessUnavailable):
        restarted.stop(
            ImageWorkerUnloadRequest(
                instance_id=uuid.uuid4(), reason="admin", requested_at=datetime.now(UTC)
            )
        )


def test_lost_stop_record_requires_matching_journal_and_empty_process_census(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, store, request = _setup(tmp_path)
    manager = supervisor.ImageProcessSupervisor(
        settings, store, lambda: 0, memory_total_bytes=10_000
    )
    manager.state_root.mkdir(mode=0o700, parents=True)
    journal_root = manager.state_root.parent / "image-jobs"
    journal_root.mkdir(mode=0o700)
    (journal_root / "prior.json").write_text("{}")
    monkeypatch.setattr(supervisor, "read_image_journal", lambda path: None)
    monkeypatch.setattr(supervisor, "_loopback_port_free", lambda port: True)
    monkeypatch.setattr(psutil, "process_iter", lambda: [])
    with pytest.raises(supervisor.ImageProcessUnavailable):
        manager.stop(_unload(request))

    monkeypatch.setattr(
        supervisor,
        "read_image_journal",
        lambda path: SimpleNamespace(request=SimpleNamespace(instance_id=request.instance_id)),
    )
    expected = [
        "python",
        "-m",
        "coire_node.image_runtime.bootstrap",
        str(manager.state_root / str(request.instance_id) / "launch.json"),
    ]
    monkeypatch.setattr(
        psutil,
        "process_iter",
        lambda: [
            SimpleNamespace(
                uids=lambda: SimpleNamespace(real=os.getuid()),
                cmdline=lambda: expected,
            )
        ],
    )
    with pytest.raises(supervisor.ImageProcessUnavailable):
        manager.stop(_unload(request))

    monkeypatch.setattr(psutil, "process_iter", lambda: [])
    stopped = manager.stop(_unload(request))
    assert stopped.safe_error == "worker_stopped"
    assert manager.stopped_path.exists()


def test_stop_escalates_to_kill_within_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, request = _started(tmp_path, monkeypatch)
    signals: list[int] = []

    def fake_signal(pid: int, sig: int) -> None:
        signals.append(sig)
        if sig == 9:
            FakePsutilProcess.alive = False

    monkeypatch.setattr("coire_node.image_runtime.supervisor.os.killpg", fake_signal)
    monkeypatch.setattr(supervisor, "_STOP_GRACE_S", 0.05)
    started = time.monotonic()
    manager.stop(_unload(request))
    assert time.monotonic() - started < 0.5
    assert signals == [15, 9]


def test_default_term_kill_grace_leaves_time_for_scheduler_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, request = _started(tmp_path, monkeypatch)
    signals: list[int] = []

    def fake_signal(pid: int, sig: int) -> None:
        assert pid == FakeProcess.pid
        signals.append(sig)
        if sig == 9:
            FakePsutilProcess.alive = False

    monkeypatch.setattr("coire_node.image_runtime.supervisor.os.killpg", fake_signal)
    started = time.monotonic()
    result = manager.stop(_unload(request))
    assert time.monotonic() - started < 3.1
    assert signals == [15, 9]
    assert result.reserved_bytes == 0


def test_worker_footprint_requires_exact_live_process_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _request = _started(tmp_path, monkeypatch)
    monkeypatch.setattr(supervisor, "measured_footprint_bytes", lambda pid: 800)
    assert manager.measured_resident_bytes() == 800
    FakePsutilProcess.alive = False
    assert manager.measured_resident_bytes() is None


def test_stop_never_signals_reused_or_uncertain_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, request = _started(tmp_path, monkeypatch)
    signals: list[int] = []
    monkeypatch.setattr(
        "coire_node.image_runtime.supervisor.os.killpg", lambda pid, sig: signals.append(sig)
    )
    FakePsutilProcess.inspection_error = True
    with pytest.raises(supervisor.ImageProcessUnavailable):
        manager.stop(_unload(request))
    assert manager.committed_bytes() == 1000 and manager.record_path.exists()
    assert signals == []
    FakePsutilProcess.inspection_error = False
    FakePsutilProcess.created = 101.0
    assert manager.stop(_unload(request)).reserved_bytes == 0
    assert signals == []


def test_restart_can_confirm_dead_record_without_releasing_uncertain_live_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, request = _started(tmp_path, monkeypatch)
    recovered = supervisor.ImageProcessSupervisor(
        manager.settings, manager.store, lambda: 0, memory_total_bytes=10_000
    )
    FakePsutilProcess.inspection_error = True
    with pytest.raises(supervisor.ImageProcessUnavailable):
        recovered.adopt_from_state()
    with pytest.raises(supervisor.ImageProcessUnavailable):
        recovered.stop(_unload(request))
    assert recovered.record_path.exists()
    assert recovered.committed_bytes() > 0
    FakePsutilProcess.inspection_error = False
    FakePsutilProcess.alive = False
    with pytest.raises(supervisor.ImageProcessUnavailable):
        recovered.stop(
            ImageWorkerUnloadRequest(
                instance_id=uuid.uuid4(), reason="admin", requested_at=datetime.now(UTC)
            )
        )
    assert recovered.record_path.exists()
    stopped = recovered.stop(_unload(request))
    assert stopped.safe_error == "worker_stopped" and stopped.reserved_bytes == 0
    assert recovered.committed_bytes() == 0
    assert not recovered.record_path.exists()
    assert recovered.stop(_unload(request)) == stopped


def test_stop_cleanup_failure_keeps_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, request = _started(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "coire_node.image_runtime.supervisor.os.killpg",
        lambda pid, sig: setattr(FakePsutilProcess, "alive", False),
    )
    monkeypatch.setattr(
        supervisor,
        "_remove_private_state",
        lambda record_path, record: (_ for _ in ()).throw(OSError("disk")),
    )
    with pytest.raises(supervisor.ImageProcessUnavailable):
        manager.stop(_unload(request))
    assert manager.committed_bytes() == 1000
    assert manager.record_path.exists()
