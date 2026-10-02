"""Fenced node dispatch and authenticated route without a native worker."""

from __future__ import annotations

import hashlib
import io
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import httpx
import pytest
from PIL import Image
from pydantic import SecretStr

from coire_core.models.image_worker import (
    ImageJobBinding,
    ImageTransferGrant,
    ImageTransferReceipt,
    ImageWorkerCancelRequest,
    ImageWorkerLoadRequest,
    ImageWorkerLoadResult,
    ImageWorkerOutputManifest,
    ImageWorkerRunRequest,
    ImageWorkerStatus,
    ImageWorkerUnloadRequest,
    NodeImageCancelRequest,
    NodeImageInputManifest,
    NodeImageJob,
    NodeImageStartRequest,
    NodeImageTransferRequest,
)
from coire_core.models.images import (
    ImageInputDigest,
    ImageMode,
    ImageSpec,
    ResolvedImageSpec,
    canonical_spec_hash,
)
from coire_core.models.node import NetworkPath, NodePath, NodeStatus
from coire_core.settings import Settings
from coire_node import image_dispatch, image_jobs
from coire_node.agent import create_app
from coire_node.image_cleanup import ImageCleanupUnavailable
from coire_node.image_dispatch import ImageNodeDispatcher
from coire_node.image_jobs import ImageJobJournal
from coire_node.image_runtime.supervisor import ImageProcessSupervisor, ImageProcessUnavailable
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


async def test_attempt_can_be_reserved_and_cancelled_before_worker_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, dispatcher = _setup(
        tmp_path, monkeypatch, httpx.MockTransport(lambda _: httpx.Response(500))
    )
    monkeypatch.setattr(dispatcher.worker, "current_status", lambda: None)
    monkeypatch.setattr(
        dispatcher.worker,
        "private_control",
        lambda instance_id: (_ for _ in ()).throw(ImageProcessUnavailable()),
    )
    app = create_app(
        settings,
        StubCollector(),
        listener=NetworkPath.CONTROL,
        store=Store(settings.node_store_dir),
        image_dispatcher=dispatcher,
    )
    command = _request()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://node",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        reserved = await client.put(
            f"/node/images/jobs/{JOB}/reserve-inputs", json=command.model_dump(mode="json")
        )
        assert reserved.status_code == 202 and reserved.json()["state"] == "queued"
        cancelled = await client.request(
            "DELETE",
            f"/node/images/jobs/{JOB}",
            json=NodeImageCancelRequest(
                job_id=JOB, attempt=1, fence=4, reason="user", requested_at=datetime.now(UTC)
            ).model_dump(mode="json"),
        )
        assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
        assert cancelled.json()["scratch_cleaned"] is True
        replay = await client.put(
            f"/node/images/jobs/{JOB}/reserve-inputs", json=command.model_dump(mode="json")
        )
        assert replay.status_code == 200 and replay.json()["state"] == "cancelled"


def test_failed_worker_cleans_both_scratch_trees_before_terminal_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, dispatcher = _setup(
        tmp_path, monkeypatch, httpx.MockTransport(lambda _: httpx.Response(503))
    )
    request = _request()
    queued = dispatcher.journal.begin(request)
    reserving = dispatcher.journal.advance(
        queued.model_copy(
            update={
                "state": "reserving",
                "pid": 123,
                "process_create_time": 1.0,
                "updated_at": queued.updated_at + timedelta(microseconds=1),
            }
        )
    )
    attempt = f"{JOB}-1-4"
    output = tmp_path / "image-scratch" / attempt
    output.mkdir(parents=True, mode=0o700)
    output.parent.chmod(0o700)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"keep")
    (output / "0.png").symlink_to(outside)
    inputs = tmp_path / "image-input-scratch" / attempt
    inputs.mkdir(parents=True, mode=0o700)
    inputs.parent.chmod(0o700)
    observed = ImageWorkerStatus(
        job_id=JOB,
        attempt=1,
        fence=4,
        state="failed",
        updated_at=datetime.now(UTC),
    )
    with pytest.raises(ImageCleanupUnavailable):
        dispatcher._reconcile(reserving, request, observed)
    assert dispatcher.journal.get(JOB) == reserving
    assert outside.read_bytes() == b"keep"


    (output / "0.png").unlink()
    (output / "0.png").write_bytes(b"partial")
    (output / "0.png").chmod(0o600)
    failed = dispatcher._reconcile(reserving, request, observed)
    assert failed.state == "failed" and failed.scratch_cleaned
    assert not output.exists() and not inputs.exists()
    assert outside.read_bytes() == b"keep"


def test_running_worker_cache_observation_reaches_fenced_node_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, dispatcher = _setup(
        tmp_path, monkeypatch, httpx.MockTransport(lambda _: httpx.Response(503))
    )
    request = _request()
    queued = dispatcher.journal.begin(request)
    reserving = dispatcher.journal.advance(
        queued.model_copy(
            update={
                "state": "reserving",
                "pid": 123,
                "process_create_time": 1.0,
                "updated_at": queued.updated_at + timedelta(microseconds=1),
            }
        )
    )
    observed = ImageWorkerStatus(
        job_id=JOB,
        attempt=1,
        fence=4,
        state="running",
        output_index=0,
        step=1,
        total_steps=20,
        cache_status="cold",
        updated_at=datetime.now(UTC),
    )
    running = dispatcher._reconcile(reserving, request, observed)
    assert running.cache_status == "cold"
    assert running.progress_step == 1
    assert dispatcher.journal.get(JOB) == running


async def test_failed_journal_repairs_cleanup_on_status_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, dispatcher = _setup(
        tmp_path, monkeypatch, httpx.MockTransport(lambda _: httpx.Response(503))
    )
    queued = dispatcher.journal.begin(_request())
    failed = dispatcher.journal.advance(
        queued.model_copy(
            update={
                "state": "failed",
                "safe_error": "generation_failed",
                "updated_at": queued.updated_at + timedelta(microseconds=1),
            }
        )
    )
    scratch = tmp_path / "image-scratch" / f"{JOB}-1-4"
    scratch.mkdir(parents=True, mode=0o700)
    scratch.parent.chmod(0o700)
    (scratch / "0.png").write_bytes(b"partial")
    (scratch / "0.png").chmod(0o600)
    repaired = await dispatcher.status(ImageJobBinding(job_id=JOB, attempt=1, fence=4))
    assert repaired is not None and repaired.state == "failed" and repaired.scratch_cleaned
    assert repaired.updated_at > failed.updated_at
    assert not scratch.exists()


async def test_cancelled_journal_repairs_cleanup_on_status_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, dispatcher = _setup(
        tmp_path, monkeypatch, httpx.MockTransport(lambda _: httpx.Response(503))
    )
    queued = dispatcher.journal.begin(_request())
    cancelled = dispatcher.journal.advance(
        queued.model_copy(
            update={
                "state": "cancelled",
                "updated_at": queued.updated_at + timedelta(microseconds=1),
            }
        )
    )
    scratch = tmp_path / "image-scratch" / f"{JOB}-1-4"
    scratch.mkdir(parents=True, mode=0o700)
    scratch.parent.chmod(0o700)
    (scratch / "0.png").write_bytes(b"partial")
    (scratch / "0.png").chmod(0o600)
    repaired = await dispatcher.status(ImageJobBinding(job_id=JOB, attempt=1, fence=4))
    assert repaired is not None and repaired.state == "cancelled" and repaired.scratch_cleaned
    assert repaired.updated_at > cancelled.updated_at
    assert not scratch.exists()


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


async def test_private_input_reservation_and_staging_are_fenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_id = uuid.uuid4()
    with io.BytesIO() as buffer:
        Image.new("RGB", (64, 64), "blue").save(buffer, format="PNG")
        payload = buffer.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    base = _request()
    spec = base.resolved.spec.model_copy(
        update={
            "mode": ImageMode.IMG2IMG,
            "init_image_id": source_id,
            "strength": Decimal("0.5"),
            "width": 64,
            "height": 64,
        }
    )
    resolved = base.resolved.model_copy(
        update={
            "spec": spec,
            "spec_hash": canonical_spec_hash(spec),
            "inputs": (ImageInputDigest(input_id=source_id, sha256=digest, width=64, height=64),),
        }
    )
    command = base.model_copy(
        update={
            "resolved": resolved,
            "inputs": (
                NodeImageInputManifest(
                    input_id=source_id,
                    purpose="init",
                    sha256=digest,
                    byte_count=len(payload),
                    width=64,
                    height=64,
                ),
            ),
        }
    )
    settings, dispatcher = _setup(
        tmp_path, monkeypatch, httpx.MockTransport(lambda _: httpx.Response(500))
    )
    app = create_app(
        settings,
        StubCollector(),
        listener=NetworkPath.CONTROL,
        store=Store(settings.node_store_dir),
        image_dispatcher=dispatcher,
    )
    reserve_url = f"/node/images/jobs/{JOB}/reserve-inputs"
    stage_url = f"/node/images/jobs/{JOB}/inputs/{source_id}"
    params: dict[str, str | int] = {
        "attempt": 1,
        "fence": 4,
        "purpose": "init",
        "sha256": digest,
        "byte_count": len(payload),
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://node"
    ) as client:
        denied = await client.put(reserve_url, json=command.model_dump(mode="json"))
        assert denied.status_code == 401
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        reserved = await client.put(reserve_url, json=command.model_dump(mode="json"))
        assert reserved.status_code == 202 and reserved.json()["state"] == "queued"
        wrong = await client.put(stage_url, params={**params, "sha256": "0" * 64}, content=payload)
        assert wrong.status_code == 409
        first = await client.put(stage_url, params=params, content=payload)
        assert first.status_code == 200 and first.json()["sha256"] == digest
        replay = await client.put(stage_url, params=params, content=payload)
        assert replay.status_code == 200

        class ExpiredClock:
            @staticmethod
            def now(tz: object) -> datetime:
                assert tz == UTC
                return command.deadline_at + timedelta(seconds=1)

        with monkeypatch.context() as patcher:
            patcher.setattr(image_jobs, "datetime", ExpiredClock)
            expired = await client.put(stage_url, params=params, content=payload)
        assert expired.status_code == 409
        assert (
            await client.put(stage_url, params={**params, "fence": 5}, content=payload)
        ).status_code == 409
        target = tmp_path / "image-input-scratch" / f"{JOB}-1-4" / str(source_id)
        assert target.read_bytes() == payload
        assert target.stat().st_mode & 0o077 == 0
        cancel = await client.request(
            "DELETE",
            f"/node/images/jobs/{JOB}",
            json=NodeImageCancelRequest(
                job_id=JOB,
                attempt=1,
                fence=4,
                reason="user",
                requested_at=datetime.now(UTC),
            ).model_dump(mode="json"),
        )
        assert cancel.status_code == 200 and cancel.json()["scratch_cleaned"] is True
    assert not target.exists()


async def test_uncertain_worker_reply_is_journaled_and_never_resent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    puts = 0

    async def worker(request: httpx.Request) -> httpx.Response:
        nonlocal puts
        if request.method == "PUT":
            puts += 1
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
        assert (await client.put(url, json=command.model_dump(mode="json"))).status_code == 503
    assert puts == 1


async def test_unconfirmed_reservation_is_delivered_once_worker_has_no_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    puts = 0
    worker_up = False

    async def worker(request: httpx.Request) -> httpx.Response:
        nonlocal puts
        if not worker_up:
            if request.method == "PUT":
                puts += 1
            raise httpx.ConnectError("worker still starting")
        if request.method == "POST":
            return httpx.Response(404, json={"detail": "worker attempt unavailable"})
        puts += 1
        command = ImageWorkerRunRequest.model_validate_json(request.content)
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
        transport=httpx.ASGITransport(app=app),
        base_url="http://node",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        assert (await client.put(url, json=command.model_dump(mode="json"))).status_code == 503
        worker_up = True
        delivered = await client.put(url, json=command.model_dump(mode="json"))
        replay = await client.put(url, json=command.model_dump(mode="json"))
    assert delivered.status_code == 202
    assert delivered.json()["state"] == "running"
    assert replay.status_code == 200
    assert replay.json()["state"] == "running"
    assert puts == 2


async def test_transfer_command_requires_node_auth_and_exact_job_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, dispatcher = _setup(
        tmp_path, monkeypatch, httpx.MockTransport(lambda _: httpx.Response(503))
    )
    app = create_app(
        settings,
        StubCollector(),
        listener=NetworkPath.CONTROL,
        store=Store(settings.node_store_dir),
        image_dispatcher=dispatcher,
    )
    now = datetime.now(UTC)
    command = NodeImageTransferRequest(
        job_id=JOB,
        attempt=1,
        fence=4,
        node=NODE,
        grants=(
            ImageTransferGrant(
                job_id=JOB,
                attempt=1,
                fence=4,
                node=NODE,
                index=0,
                expected_bytes=100,
                expected_sha256="a" * 64,
                token="transfer-token",
                issued_at=now,
                expires_at=now + timedelta(minutes=1),
            ),
        ),
    )
    url = f"/node/images/jobs/{JOB}/transfer"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://node"
    ) as client:
        assert (await client.post(url, json=command.model_dump(mode="json"))).status_code == 401
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        assert (
            await client.post(
                f"/node/images/jobs/{uuid.uuid4()}/transfer",
                json=command.model_dump(mode="json"),
            )
        ).status_code == 409
        assert (await client.post(url, json=command.model_dump(mode="json"))).status_code == 409


async def test_successful_transfer_discards_staged_inputs_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, dispatcher = _setup(
        tmp_path, monkeypatch, httpx.MockTransport(lambda _: httpx.Response(503))
    )
    original = _request()
    now = datetime.now(UTC)
    receipt = ImageTransferReceipt(
        job_id=JOB,
        attempt=1,
        fence=4,
        node=NODE,
        index=0,
        output_id=uuid.uuid4(),
        byte_count=100,
        sha256="a" * 64,
        recipe_sha256="b" * 64,
        verified_at=now,
    )
    current = NodeImageJob(
        job_id=JOB,
        attempt=1,
        fence=4,
        node=NODE,
        instance_id=INSTANCE,
        state="transferring",
        outputs=(
            ImageWorkerOutputManifest(
                index=0, byte_count=100, sha256="a" * 64, recipe_sha256="b" * 64
            ),
        ),
        receipts=(receipt,),
        updated_at=now,
    )
    finished = current.model_copy(update={"state": "succeeded", "scratch_cleaned": True})
    state = {"job": current}
    events: list[str] = []
    monkeypatch.setattr(dispatcher.journal, "get", lambda job_id: state["job"])
    monkeypatch.setattr(dispatcher.journal, "request", lambda job_id: original)

    def discard(*args: object) -> None:
        del args
        events.append("inputs")

    def cleanup(*args: object) -> None:
        del args
        events.append("outputs")
        state["job"] = finished

    monkeypatch.setattr(image_dispatch, "discard_node_image_inputs", discard)
    monkeypatch.setattr(image_dispatch, "cleanup_image_outputs", cleanup)
    command = NodeImageTransferRequest(
        job_id=JOB,
        attempt=1,
        fence=4,
        node=NODE,
        grants=(
            ImageTransferGrant(
                job_id=JOB,
                attempt=1,
                fence=4,
                node=NODE,
                index=0,
                expected_bytes=100,
                expected_sha256="a" * 64,
                token="transfer-token",
                issued_at=now,
                expires_at=now + timedelta(minutes=1),
            ),
        ),
    )
    result = await dispatcher.transfer(command)
    assert result.state == "succeeded"
    assert events == ["inputs", "outputs"]


@pytest.mark.parametrize("healthy", [True, False])
async def test_adopted_starting_worker_rechecks_health_before_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, healthy: bool
) -> None:
    observations = 0
    health_checks = 0
    revalidated = False

    def observe(request: httpx.Request) -> httpx.Response:
        nonlocal observations
        assert request.method == "POST" and request.url.path == "/status"
        observations += 1
        return httpx.Response(
            200,
            json=ImageWorkerStatus(
                job_id=JOB, attempt=1, fence=4, state="running", updated_at=datetime.now(UTC)
            ).model_dump(mode="json"),
        )

    _, dispatcher = _setup(tmp_path, monkeypatch, httpx.MockTransport(observe))
    load, port, token = dispatcher.worker.private_control(INSTANCE)
    ready = dispatcher.worker.current_status()
    assert ready is not None
    starting = ready.model_copy(update={"state": "starting"})
    monkeypatch.setattr(dispatcher.worker, "current_status", lambda: starting)

    async def refresh(client: httpx.AsyncClient) -> ImageWorkerLoadResult:
        nonlocal health_checks, revalidated
        assert isinstance(client, httpx.AsyncClient)
        health_checks += 1
        revalidated = healthy
        return ready if healthy else starting

    def private(instance_id: uuid.UUID) -> tuple[ImageWorkerLoadRequest, int, str]:
        assert instance_id == INSTANCE
        if not revalidated:
            raise ImageProcessUnavailable()
        return load, port, token

    monkeypatch.setattr(dispatcher.worker, "refresh_ready", refresh)
    monkeypatch.setattr(dispatcher.worker, "private_control", private)
    current = dispatcher.journal.begin(_request())
    if healthy:
        observed = await dispatcher._worker_status(current)
        assert observed is not None and observed.state == "running"
        assert observations == 1
    else:
        with pytest.raises(image_dispatch.ImageDispatchUnavailable):
            await dispatcher._worker_status(current)
        assert observations == 0
    assert health_checks == 1
    assert dispatcher.journal.get(JOB) == current


async def test_status_reconciles_after_restart_without_generation_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    starts = 0
    observed_step = 3

    async def worker(request: httpx.Request) -> httpx.Response:
        nonlocal starts
        if request.method == "PUT":
            starts += 1
            start_binding = ImageWorkerRunRequest.model_validate_json(request.content)
            payload = ImageWorkerStatus(
                job_id=start_binding.job_id,
                attempt=start_binding.attempt,
                fence=start_binding.fence,
                state="running",
                updated_at=datetime.now(UTC),
            )
            return httpx.Response(202, json=payload.model_dump(mode="json"))
        assert request.method == "POST" and request.url.path == "/status"
        binding = ImageJobBinding.model_validate_json(request.content)
        if observed_step <= 20:
            payload = ImageWorkerStatus(
                job_id=binding.job_id,
                attempt=binding.attempt,
                fence=binding.fence,
                state="running",
                output_index=0,
                step=observed_step,
                total_steps=20,
                updated_at=datetime.now(UTC),
            )
        else:
            payload = ImageWorkerStatus(
                job_id=binding.job_id,
                attempt=binding.attempt,
                fence=binding.fence,
                state="generated",
                outputs=(
                    ImageWorkerOutputManifest(
                        index=0,
                        byte_count=100,
                        sha256="c" * 64,
                        recipe_sha256="d" * 64,
                    ),
                ),
                updated_at=datetime.now(UTC),
            )
        return httpx.Response(200, json=payload.model_dump(mode="json"))

    transport = httpx.MockTransport(worker)
    settings, dispatcher = _setup(tmp_path, monkeypatch, transport)
    command = _request()
    app = create_app(
        settings,
        StubCollector(),
        listener=NetworkPath.CONTROL,
        store=Store(settings.node_store_dir),
        image_dispatcher=dispatcher,
    )
    url = f"/node/images/jobs/{JOB}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://node"
    ) as anonymous:
        assert (await anonymous.get(f"{url}?attempt=1&fence=4")).status_code == 401
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://node",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        assert (await client.put(url, json=command.model_dump(mode="json"))).status_code == 202
        assert (await client.get(f"{url}?attempt=1&fence=5")).status_code == 409
        assert (await client.get(f"{url}?attempt=1&fence=4")).json()["progress_step"] == 3
        observed_step = 21
        completed = await client.get(f"{url}?attempt=1&fence=4")
        assert completed.status_code == 200
        assert completed.json()["state"] == "transferring"
    restarted = ImageNodeDispatcher(
        ImageJobJournal(tmp_path, NODE), dispatcher.worker, transport=transport
    )
    app = create_app(
        settings,
        StubCollector(),
        listener=NetworkPath.CONTROL,
        store=Store(settings.node_store_dir),
        image_dispatcher=restarted,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://node",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        recovered = await client.get(f"{url}?attempt=1&fence=4")
        assert recovered.json()["state"] == "transferring"
        assert recovered.json()["outputs"][0]["sha256"] == "c" * 64

        def missing_worker(_: uuid.UUID) -> None:
            raise ImageProcessUnavailable()

        monkeypatch.setattr(restarted.worker, "private_control", missing_worker)
        persisted = await client.get(f"{url}?attempt=1&fence=4")
        assert persisted.status_code == 200
        assert persisted.json()["outputs"] == recovered.json()["outputs"]
    assert starts == 1


async def test_forged_worker_status_does_not_change_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def worker(request: httpx.Request) -> httpx.Response:
        binding = (
            ImageWorkerRunRequest.model_validate_json(request.content)
            if request.method == "PUT"
            else None
        )
        return httpx.Response(
            200,
            json=ImageWorkerStatus(
                job_id=binding.job_id if binding else JOB,
                attempt=1,
                fence=5 if binding is None else 4,
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
        transport=httpx.ASGITransport(app=app),
        base_url="http://node",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        assert (await client.put(url, json=command.model_dump(mode="json"))).status_code == 202
        before = dispatcher.journal.get(JOB)
        assert (await client.get(f"{url}?attempt=1&fence=4")).status_code == 503
        assert dispatcher.journal.get(JOB) == before


async def test_terminal_journal_status_needs_no_live_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unexpected_worker(request: httpx.Request) -> httpx.Response:
        raise AssertionError("terminal status must not contact worker")

    settings, dispatcher = _setup(tmp_path, monkeypatch, httpx.MockTransport(unexpected_worker))
    queued = dispatcher.journal.begin(_request())
    failed = dispatcher.journal.advance(
        queued.model_copy(update={"state": "failed", "updated_at": datetime.now(UTC)})
    )
    app = create_app(
        settings,
        StubCollector(),
        listener=NetworkPath.CONTROL,
        store=Store(settings.node_store_dir),
        image_dispatcher=dispatcher,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://node",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        response = await client.get(f"/node/images/jobs/{JOB}?attempt=1&fence=4")
    assert response.status_code == 200
    assert response.json()["state"] == failed.state


@pytest.mark.parametrize("cooperative", [True, False])
async def test_cancel_confirms_worker_or_exact_process_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cooperative: bool
) -> None:
    stop_calls: list[ImageWorkerUnloadRequest] = []

    async def worker(request: httpx.Request) -> httpx.Response:
        binding: ImageJobBinding
        if request.method == "PUT":
            binding = ImageWorkerRunRequest.model_validate_json(request.content)
        elif request.url.path == "/cancel":
            binding = ImageWorkerCancelRequest.model_validate_json(request.content)
        else:
            binding = ImageJobBinding.model_validate_json(request.content)
        state: Literal["cancelled", "running"] = (
            "cancelled" if request.url.path == "/cancel" and cooperative else "running"
        )
        return httpx.Response(
            200,
            json=ImageWorkerStatus(
                job_id=binding.job_id,
                attempt=binding.attempt,
                fence=binding.fence,
                state=state,
                updated_at=datetime.now(UTC),
            ).model_dump(mode="json"),
        )

    settings, dispatcher = _setup(tmp_path, monkeypatch, httpx.MockTransport(worker))

    def fake_stop(request: ImageWorkerUnloadRequest) -> ImageWorkerLoadResult:
        stop_calls.append(request)
        return ImageWorkerLoadResult(
            instance_id=INSTANCE, state="failed", reserved_bytes=0, safe_error="worker_stopped"
        )

    monkeypatch.setattr(dispatcher.worker, "stop", fake_stop)
    app = create_app(
        settings,
        StubCollector(),
        listener=NetworkPath.CONTROL,
        store=Store(settings.node_store_dir),
        image_dispatcher=dispatcher,
    )
    command = _request()
    cancel = NodeImageCancelRequest(
        job_id=JOB, attempt=1, fence=4, reason="user", requested_at=datetime.now(UTC)
    )
    url = f"/node/images/jobs/{JOB}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://node"
    ) as anonymous:
        assert (
            await anonymous.request("DELETE", url, json=cancel.model_dump(mode="json"))
        ).status_code == 401
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://node",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        assert (await client.put(url, json=command.model_dump(mode="json"))).status_code == 202
        scratch = tmp_path / "image-scratch" / f"{JOB}-1-4"
        scratch.mkdir(parents=True, mode=0o700)
        scratch.parent.chmod(0o700)
        output = scratch / "0.png"
        output.write_bytes(b"private unfinished output")
        output.chmod(0o600)
        wrong = cancel.model_copy(update={"fence": 5})
        assert (
            await client.request("DELETE", url, json=wrong.model_dump(mode="json"))
        ).status_code == 409
        result = await client.request("DELETE", url, json=cancel.model_dump(mode="json"))
        assert result.status_code == 200
        assert result.json()["state"] == "cancelled"
        assert result.json()["scratch_cleaned"] is True
        assert not scratch.exists()
        assert (await client.request("DELETE", url, json=cancel.model_dump(mode="json"))).json()[
            "state"
        ] == "cancelled"
    assert len(stop_calls) == (0 if cooperative else 1)
    assert dispatcher.journal.get(JOB).state == "cancelled"  # type: ignore[union-attr]


async def test_cancel_uncertain_process_keeps_journal_and_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def worker(request: httpx.Request) -> httpx.Response:
        binding: ImageJobBinding
        if request.method == "PUT":
            binding = ImageWorkerRunRequest.model_validate_json(request.content)
        elif request.url.path == "/cancel":
            binding = ImageWorkerCancelRequest.model_validate_json(request.content)
        else:
            binding = ImageJobBinding.model_validate_json(request.content)
        return httpx.Response(
            200,
            json=ImageWorkerStatus(
                job_id=binding.job_id,
                attempt=binding.attempt,
                fence=binding.fence,
                state="running",
                updated_at=datetime.now(UTC),
            ).model_dump(mode="json"),
        )

    settings, dispatcher = _setup(tmp_path, monkeypatch, httpx.MockTransport(worker))

    def uncertain_stop(request: ImageWorkerUnloadRequest) -> ImageWorkerLoadResult:
        raise ImageProcessUnavailable()

    monkeypatch.setattr(dispatcher.worker, "stop", uncertain_stop)
    app = create_app(
        settings,
        StubCollector(),
        listener=NetworkPath.CONTROL,
        store=Store(settings.node_store_dir),
        image_dispatcher=dispatcher,
    )
    cancel = NodeImageCancelRequest(
        job_id=JOB, attempt=1, fence=4, reason="user", requested_at=datetime.now(UTC)
    )
    url = f"/node/images/jobs/{JOB}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://node",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        assert (await client.put(url, json=_request().model_dump(mode="json"))).status_code == 202
        assert (
            await client.request("DELETE", url, json=cancel.model_dump(mode="json"))
        ).status_code == 503
    assert dispatcher.journal.get(JOB).state == "cancelling"  # type: ignore[union-attr]


async def test_queued_cancel_is_local_and_never_calls_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unexpected_worker(request: httpx.Request) -> httpx.Response:
        raise AssertionError("queued cancel must not contact worker")

    settings, dispatcher = _setup(tmp_path, monkeypatch, httpx.MockTransport(unexpected_worker))
    dispatcher.journal.begin(_request())
    app = create_app(
        settings,
        StubCollector(),
        listener=NetworkPath.CONTROL,
        store=Store(settings.node_store_dir),
        image_dispatcher=dispatcher,
    )
    cancel = NodeImageCancelRequest(
        job_id=JOB, attempt=1, fence=4, reason="user", requested_at=datetime.now(UTC)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://node",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        response = await client.request(
            "DELETE", f"/node/images/jobs/{JOB}", json=cancel.model_dump(mode="json")
        )
    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"
    assert response.json()["scratch_cleaned"] is True


async def test_queued_cancel_retries_unsafe_scratch_without_marking_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, dispatcher = _setup(
        tmp_path, monkeypatch, httpx.MockTransport(lambda _: httpx.Response(503))
    )
    queued = dispatcher.journal.begin(_request())
    scratch = tmp_path / "image-scratch" / f"{JOB}-1-4"
    scratch.mkdir(parents=True, mode=0o700)
    scratch.parent.chmod(0o700)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"keep")
    (scratch / "0.png").symlink_to(outside)
    app = create_app(
        settings,
        StubCollector(),
        listener=NetworkPath.CONTROL,
        store=Store(settings.node_store_dir),
        image_dispatcher=dispatcher,
    )
    cancel = NodeImageCancelRequest(
        job_id=JOB, attempt=1, fence=4, reason="user", requested_at=datetime.now(UTC)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://node",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        url = f"/node/images/jobs/{JOB}"
        first = await client.request("DELETE", url, json=cancel.model_dump(mode="json"))
        assert first.status_code == 503
        assert dispatcher.journal.get(JOB) == queued
        assert outside.read_bytes() == b"keep"
        (scratch / "0.png").unlink()
        second = await client.request("DELETE", url, json=cancel.model_dump(mode="json"))
        assert second.status_code == 200
        assert second.json()["scratch_cleaned"] is True
        assert not scratch.exists()


async def test_legacy_cancelled_journal_repairs_cleanup_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, dispatcher = _setup(
        tmp_path, monkeypatch, httpx.MockTransport(lambda _: httpx.Response(503))
    )
    queued = dispatcher.journal.begin(_request())
    dispatcher.journal.advance(
        queued.model_copy(
            update={
                "state": "cancelled",
                "updated_at": queued.updated_at + timedelta(microseconds=1),
            }
        )
    )
    app = create_app(
        settings,
        StubCollector(),
        listener=NetworkPath.CONTROL,
        store=Store(settings.node_store_dir),
        image_dispatcher=dispatcher,
    )
    cancel = NodeImageCancelRequest(
        job_id=JOB, attempt=1, fence=4, reason="user", requested_at=datetime.now(UTC)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://node",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        response = await client.request(
            "DELETE", f"/node/images/jobs/{JOB}", json=cancel.model_dump(mode="json")
        )
    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"
    assert response.json()["scratch_cleaned"] is True
