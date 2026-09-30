"""Authenticated node image worker control and shared engine budget."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from coire_core.models.image_worker import (
    ImageWorkerLoadRequest,
    ImageWorkerLoadResult,
    ImageWorkerUnloadRequest,
)
from coire_core.models.node import NetworkPath, NodePath, NodeStatus
from coire_core.settings import Settings
from coire_node.agent import create_app
from coire_node.engines import BudgetExceeded, EngineManager
from coire_node.image_runtime.supervisor import ImageProcessSupervisor, ImageProcessUnavailable
from coire_node.store import Store

TOKEN = "node-secret"
SLUG = "studio--z-image-turbo"


class StubCollector:
    def latest(self, *, path: NodePath = NodePath.MESH) -> NodeStatus:
        raise AssertionError("not called")


def _fixture(tmp_path: Path) -> tuple[Settings, Store, ImageWorkerLoadRequest]:
    settings = Settings(
        _secrets_dir="/nonexistent",  # type: ignore[call-arg]
        node_token=SecretStr(TOKEN),
        node_store_dir=str(tmp_path / "models"),
        node_state_dir=str(tmp_path / "state"),
        node_image_worker_port=9600,
        node_memory_budget_fraction=0.9,
    )
    store = Store(settings.node_store_dir)
    model_dir = store.path_for(SLUG)
    model_dir.mkdir(parents=True)
    (model_dir / "config.json").write_text("{}")
    (model_dir / "model.safetensors").write_bytes(b"safe")
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


async def test_node_routes_require_bearer_and_exact_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, store, request = _fixture(tmp_path)
    manager = ImageProcessSupervisor(settings, store, lambda: 0, memory_total_bytes=10_000)
    starting = ImageWorkerLoadResult(
        instance_id=request.instance_id,
        state="starting",
        pid=123,
        process_create_time=1.0,
        port=9600,
        reserved_bytes=1000,
    )
    ready = starting.model_copy(update={"state": "ready"})
    stopped = ImageWorkerLoadResult(
        instance_id=request.instance_id,
        state="failed",
        reserved_bytes=0,
        safe_error="worker_stopped",
    )
    calls: list[str] = []

    def fake_start(body: ImageWorkerLoadRequest) -> ImageWorkerLoadResult:
        calls.append("start")
        return starting

    monkeypatch.setattr(manager, "start", fake_start)
    monkeypatch.setattr(manager, "current_status", lambda: starting)

    async def fake_refresh(client: httpx.AsyncClient) -> ImageWorkerLoadResult:
        calls.append("refresh")
        return ready

    monkeypatch.setattr(manager, "refresh_ready", fake_refresh)

    def fake_stop(body: ImageWorkerUnloadRequest) -> ImageWorkerLoadResult:
        calls.append("stop")
        return stopped

    monkeypatch.setattr(manager, "stop", fake_stop)
    app = create_app(
        settings,
        StubCollector(),
        listener=NetworkPath.CONTROL,
        store=store,
        image_workers=manager,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://node") as client:
        url = "/node/images/worker"
        assert (await client.put(url, json=request.model_dump(mode="json"))).status_code == 401
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        started = await client.put(url, json=request.model_dump(mode="json"))
        assert started.status_code == 202
        assert ImageWorkerLoadResult.model_validate(started.json()) == starting
        got = await client.get(f"{url}/{request.instance_id}")
        assert ImageWorkerLoadResult.model_validate(got.json()) == ready
        assert (await client.get(f"{url}/{uuid.uuid4()}")).status_code == 404
        unload = ImageWorkerUnloadRequest(
            instance_id=request.instance_id, reason="admin", requested_at=datetime.now(UTC)
        )
        assert (
            await client.request(
                "DELETE", f"{url}/{uuid.uuid4()}", json=unload.model_dump(mode="json")
            )
        ).status_code == 409
        deleted = await client.request(
            "DELETE", f"{url}/{request.instance_id}", json=unload.model_dump(mode="json")
        )
        assert ImageWorkerLoadResult.model_validate(deleted.json()) == stopped
    assert calls == ["start", "refresh", "stop"]
    data_app = create_app(
        settings,
        StubCollector(),
        listener=NetworkPath.DATA,
        store=store,
        image_workers=manager,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=data_app),
        base_url="http://node",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        assert (
            await client.put("/node/images/worker", json=request.model_dump(mode="json"))
        ).status_code == 404


def test_image_and_language_loads_include_each_others_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, store, request = _fixture(tmp_path)
    lock = threading.RLock()
    engine = EngineManager(
        settings,
        store,
        "127.0.0.1",
        memory_lock=lock,
        additional_committed_bytes=lambda: 8500,
    )
    engine._memory_total = 10_000
    with pytest.raises(BudgetExceeded):
        engine.start(engine_id=uuid.uuid4(), slug=SLUG, estimate_bytes=1000)
    images = ImageProcessSupervisor(
        settings, store, lambda: 8500, memory_total_bytes=10_000, memory_lock=lock
    )
    with pytest.raises(ImageProcessUnavailable):
        images.start(request)
    assert images.committed_bytes() == 0
