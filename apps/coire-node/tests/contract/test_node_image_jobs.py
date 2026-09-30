"""Fenced node dispatch and authenticated route without a native worker."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from coire_core.models.image_worker import (
    ImageWorkerLoadRequest,
    ImageWorkerLoadResult,
    ImageWorkerRunRequest,
    ImageWorkerStatus,
    NodeImageStartRequest,
)
from coire_core.models.images import ImageSpec, ResolvedImageSpec, canonical_spec_hash
from coire_core.models.node import NetworkPath, NodePath, NodeStatus
from coire_core.settings import Settings
from coire_node.agent import create_app
from coire_node.image_dispatch import ImageNodeDispatcher
from coire_node.image_jobs import ImageJobJournal
from coire_node.image_runtime.supervisor import ImageProcessSupervisor
from coire_node.store import Store

JOB = "01J00000000000000000000000"
NODE = "coire-edge-b"
TOKEN = "node-secret"
MODEL = uuid.UUID("10000000-0000-0000-0000-000000000001")
INSTANCE = uuid.UUID("20000000-0000-0000-0000-000000000001")


class StubCollector:
    def latest(self, *, path: NodePath = NodePath.MESH) -> NodeStatus:
        raise AssertionError("not called")


def _request() -> NodeImageStartRequest:
    spec = ImageSpec(
        model_id=MODEL,
        prompt="fox",
        width=512,
        height=512,
        steps=20,
        guidance=Decimal("0"),
        seed=7,
    )
    resolved = ResolvedImageSpec(
        spec=spec,
        seeds=(7,),
        pipeline_version="mflux-0.20.0",
        environment_fingerprint="a" * 64,
        model_sha256="b" * 64,
        spec_hash=canonical_spec_hash(spec),
    )
    return NodeImageStartRequest(
        job_id=JOB,
        attempt=1,
        fence=4,
        node=NODE,
        model_id=MODEL,
        instance_id=INSTANCE,
        resolved=resolved,
        deadline_at=datetime.now(UTC) + timedelta(minutes=2),
        reservation_bytes=1024,
    )


def _setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker_handler: httpx.AsyncBaseTransport,
) -> tuple[Settings, ImageNodeDispatcher]:
    settings = Settings(
        _secrets_dir="/nonexistent",  # type: ignore[call-arg]
        node_name=NODE,
        node_token=SecretStr(TOKEN),
        node_state_dir=str(tmp_path),
        node_store_dir=str(tmp_path / "models"),
    )
    supervisor = ImageProcessSupervisor(settings, Store(settings.node_store_dir), lambda: 0)
    load = ImageWorkerLoadRequest(
        slug="studio--z-image-turbo",
        model_id=MODEL,
        instance_id=INSTANCE,
        manifest_sha256="b" * 64,
        reservation_bytes=1024,
        runtime_version="mflux-0.20.0",
    )
    ready = ImageWorkerLoadResult(
        instance_id=INSTANCE,
        state="ready",
        pid=123,
        process_create_time=1.0,
        port=9600,
        reserved_bytes=1024,
    )
    monkeypatch.setattr(
        supervisor, "private_control", lambda instance_id: (load, 9600, "private-token")
    )
    monkeypatch.setattr(supervisor, "current_status", lambda: ready)
    journal = ImageJobJournal(tmp_path, NODE)
    return settings, ImageNodeDispatcher(journal, supervisor, transport=worker_handler)


async def test_dispatch_auth_replay_and_exact_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[ImageWorkerRunRequest] = []

    async def worker(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "127.0.0.1"
        assert request.headers["Authorization"] == "Bearer private-token"
        command = ImageWorkerRunRequest.model_validate_json(request.content)
        commands.append(command)
        return httpx.Response(
            202,
            json=ImageWorkerStatus(
                job_id=command.job_id,
                attempt=command.attempt,
                fence=command.fence,
                state="running",
                updated_at=datetime.now(UTC),
            ).model_dump(mode="json"),
        )

    settings, dispatcher = _setup(tmp_path, monkeypatch, httpx.MockTransport(worker))
    app = create_app(
        settings,
        StubCollector(),
        listener=NetworkPath.CONTROL,
        store=Store(settings.node_store_dir),
        image_dispatcher=dispatcher,
    )
    command = _request()
    url = f"/node/images/jobs/{JOB}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://node"
    ) as client:
        assert (await client.put(url, json=command.model_dump(mode="json"))).status_code == 401
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        first = await client.put(url, json=command.model_dump(mode="json"))
        assert first.status_code == 202
        assert first.json()["state"] == "running"
        replay = await client.put(url, json=command.model_dump(mode="json"))
        assert replay.status_code == 200
        assert replay.json() == first.json()
        changed = command.model_copy(update={"fence": 5})
        assert (await client.put(url, json=changed.model_dump(mode="json"))).status_code == 409
        assert (
            await client.put(
                f"/node/images/jobs/{uuid.uuid4()}", json=command.model_dump(mode="json")
            )
        ).status_code == 409
    assert len(commands) == 1
    assert dispatcher.journal.get(JOB) is not None
    data_app = create_app(
        settings,
        StubCollector(),
        listener=NetworkPath.DATA,
        store=Store(settings.node_store_dir),
        image_dispatcher=dispatcher,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=data_app),
        base_url="http://node",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        assert (await client.put(url, json=command.model_dump(mode="json"))).status_code == 404


async def test_uncertain_worker_reply_is_journaled_and_never_resent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def worker(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("private worker disappeared")

    settings, dispatcher = _setup(tmp_path, monkeypatch, httpx.MockTransport(worker))
    app = create_app(
        settings,
        StubCollector(),
        listener=NetworkPath.CONTROL,
        store=Store(settings.node_store_dir),
        image_dispatcher=dispatcher,
    )
    command = _request()
    url = f"/node/images/jobs/{JOB}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://node",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        assert (await client.put(url, json=command.model_dump(mode="json"))).status_code == 503
        assert dispatcher.journal.get(JOB).state == "reserving"  # type: ignore[union-attr]
        assert (await client.put(url, json=command.model_dump(mode="json"))).status_code == 200
    assert calls == 1
