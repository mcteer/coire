"""Image dispatch prefers Studio B and does not regenerate a lost attempt."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.nodes_client import NodeError, NodeErrorKind
from coire_core.errors import ImageConflict
from coire_core.models.image_worker import (
    ImageWorkerLoadRequest,
    ImageWorkerLoadResult,
    NodeImageInputManifest,
    NodeImageInputRequest,
    NodeImageJob,
    NodeImageStartRequest,
)
from coire_core.models.images import (
    ImageInputDigest,
    ImageMode,
    ImageSpec,
    ResolvedImageSpec,
    canonical_spec_hash,
    expand_image_seeds,
)
from coire_core.settings import Settings
from coire_scheduler import image_dispatch, images
from coire_scheduler.image_dispatch import (
    IMAGE_PREFERRED_NODE,
    ImageNodeCandidate,
    PreparedImageDispatch,
    choose_image_node,
)

JOB = "01J00000000000000000000000"
NODE = "coire-edge-b"
INSTANCE = uuid.uuid4()
MODEL = uuid.uuid4()


def _candidate(name: str, **overrides: object) -> ImageNodeCandidate:
    values: dict[str, object] = {
        "name": name,
        "node_id": uuid.uuid4(),
        "healthy": True,
        "memory_total_bytes": 64 * 1024**3,
        "image_busy": False,
        "chat_unmeasured": False,
    }
    values.update(overrides)
    return ImageNodeCandidate(**values)  # type: ignore[arg-type]


def test_auto_placement_prefers_studio_b_and_skips_blocked_nodes() -> None:
    preferred = _candidate(IMAGE_PREFERRED_NODE)
    other = _candidate("coire-edge-a")
    chosen = choose_image_node("single:auto", 1024, [other, preferred])
    assert chosen is preferred
    busy = choose_image_node(
        "single:auto", 1024, [_candidate(IMAGE_PREFERRED_NODE, image_busy=True), other]
    )
    assert busy is other
    waiting = choose_image_node(
        "single:auto",
        1024,
        [
            _candidate(IMAGE_PREFERRED_NODE, chat_unmeasured=True),
            _candidate("coire-edge-a", chat_unmeasured=True),
        ],
    )
    assert waiting is None
    pinned = choose_image_node("pinned:coire-edge-a", 1024, [preferred, other])
    assert pinned is other
    unreachable = choose_image_node(
        "pinned:coire-edge-b", 1024, [_candidate(IMAGE_PREFERRED_NODE, healthy=False), other]
    )
    assert unreachable is None
    overheated = choose_image_node(
        "single:auto", 1024, [_candidate(IMAGE_PREFERRED_NODE, thermal_alarm=True), other]
    )
    assert overheated is other
    assert (
        choose_image_node(
            "pinned:coire-edge-b", 1024, [_candidate(IMAGE_PREFERRED_NODE, thermal_alarm=True)]
        )
        is None
    )


async def test_draining_worker_blocks_new_image_placement() -> None:
    class Session:
        def __init__(self) -> None:
            self.queries: list[str] = []

        async def scalar(self, statement: object) -> object | None:
            self.queries.append(str(statement))
            return "draining-instance" if len(self.queries) == 2 else None

    session = Session()
    assert await image_dispatch._image_busy(cast(AsyncSession, session), uuid.uuid4(), MODEL)
    assert len(session.queries) == 2
    assert "image_execution_leases" in session.queries[0]
    assert "model_instances.state" in session.queries[1]


def _prepared() -> PreparedImageDispatch:
    spec = ImageSpec(
        model_id=MODEL,
        prompt="private subject",
        width=64,
        height=64,
        steps=2,
        guidance=Decimal(0),
        seed=3,
    )
    resolved = ResolvedImageSpec(
        spec=spec,
        seeds=tuple(expand_image_seeds(3, 1)),
        pipeline_version="mflux-0.20.0",
        environment_fingerprint="a" * 64,
        model_sha256="b" * 64,
        spec_hash=canonical_spec_hash(spec),
    )
    load = ImageWorkerLoadRequest(
        slug="flux--schnell",
        model_id=MODEL,
        instance_id=INSTANCE,
        manifest_sha256="b" * 64,
        reservation_bytes=1024,
        runtime_version="mflux-0.20.0",
    )
    start = NodeImageStartRequest(
        job_id=JOB,
        attempt=1,
        fence=1,
        node=NODE,
        model_id=MODEL,
        instance_id=INSTANCE,
        resolved=resolved,
        deadline_at=datetime.now(UTC),
        reservation_bytes=1024,
    )
    return PreparedImageDispatch(node=NODE, load=load, start=start)


@pytest.mark.parametrize(
    ("missing", "bad_ready", "terminal_reservation"),
    [(False, False, False), (True, False, False), (False, True, False), (False, False, True)],
)
async def test_dispatch_retains_placed_holds_when_a_worker_reply_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    missing: bool,
    bad_ready: bool,
    terminal_reservation: bool,
) -> None:
    prepared = _prepared()
    calls: list[str] = []

    async def prepare(session: object, job_id: str, settings: Settings) -> PreparedImageDispatch:
        del session, settings
        assert job_id == JOB
        calls.append("prepare")
        return prepared

    class Client:
        def __init__(self, settings: Settings) -> None:
            del settings

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def reserve_image_inputs(
            self, node: str, command: NodeImageStartRequest
        ) -> NodeImageJob:
            assert node == NODE and command.job_id == JOB
            calls.append("reserve")
            return NodeImageJob(
                job_id=JOB,
                attempt=1,
                fence=1,
                node=NODE,
                instance_id=INSTANCE,
                state="cancelled" if terminal_reservation else "queued",
                scratch_cleaned=terminal_reservation,
                updated_at=datetime.now(UTC),
            )

        async def load_image_worker(
            self, node: str, command: ImageWorkerLoadRequest
        ) -> ImageWorkerLoadResult:
            assert node == NODE and command.instance_id == INSTANCE
            calls.append("load")
            return ImageWorkerLoadResult(
                instance_id=INSTANCE, state="starting", reserved_bytes=1024
            )

        async def image_worker_status(
            self, node: str, instance_id: uuid.UUID
        ) -> ImageWorkerLoadResult:
            assert node == NODE and instance_id == INSTANCE
            calls.append("ready")
            return ImageWorkerLoadResult(
                instance_id=INSTANCE,
                state="ready",
                reserved_bytes=1025 if bad_ready else 1024,
                pid=123,
                process_create_time=100.0,
                port=9600,
            )

        async def start_image_job(self, node: str, command: NodeImageStartRequest) -> NodeImageJob:
            calls.append("start")
            if missing:
                raise NodeError(NodeErrorKind.NOT_FOUND, node, status=404)
            return NodeImageJob(
                job_id=command.job_id,
                attempt=command.attempt,
                fence=command.fence,
                node=node,
                instance_id=command.instance_id,
                state="reserving",
                updated_at=datetime.now(UTC),
            )

    @asynccontextmanager
    async def scope() -> AsyncIterator[object]:
        yield object()

    monkeypatch.setattr(images, "prepare_image_dispatch", prepare)
    monkeypatch.setattr(images, "NodeClient", Client)
    monkeypatch.setattr(images, "session_scope", scope)
    monkeypatch.setattr(images, "get_settings", lambda: Settings(_secrets_dir="/nonexistent"))  # type: ignore[call-arg]

    if terminal_reservation:
        assert await images.drive_image_dispatch(JOB) is True
        assert calls == ["prepare", "reserve"]
    elif bad_ready:
        with pytest.raises(ImageConflict, match="readiness differs"):
            await images.drive_image_dispatch(JOB)
        assert calls == ["prepare", "reserve", "load", "ready"]
    elif missing:
        with pytest.raises(ImageConflict, match="journal is unavailable"):
            await images.drive_image_dispatch(JOB)
        assert calls == ["prepare", "reserve", "load", "ready", "start"]
    else:
        assert await images.drive_image_dispatch(JOB) is True
        assert calls == ["prepare", "reserve", "load", "ready", "start"]
        assert await images.drive_image_dispatch(JOB) is True
        assert calls.count("start") == 2


async def test_img2img_dispatch_reserves_and_stages_exact_owner_input_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_id = uuid.uuid4()
    payload = b"normalized-private-png"
    digest = hashlib.sha256(payload).hexdigest()
    source = tmp_path / str(input_id)
    source.write_bytes(payload)
    basic = _prepared()
    spec = basic.start.resolved.spec.model_copy(
        update={
            "mode": ImageMode.IMG2IMG,
            "init_image_id": input_id,
            "strength": Decimal("0.375125"),
        }
    )
    resolved = basic.start.resolved.model_copy(
        update={
            "spec": spec,
            "spec_hash": canonical_spec_hash(spec),
            "inputs": (ImageInputDigest(input_id=input_id, sha256=digest, width=64, height=64),),
        }
    )
    manifest = NodeImageInputManifest(
        input_id=input_id,
        purpose="init",
        sha256=digest,
        byte_count=len(payload),
        width=64,
        height=64,
    )
    prepared = PreparedImageDispatch(
        node=NODE,
        load=basic.load,
        start=basic.start.model_copy(update={"resolved": resolved, "inputs": (manifest,)}),
    )
    calls: list[str] = []

    async def prepare(*_: object) -> PreparedImageDispatch:
        return prepared

    class Session:
        async def get(self, model: object, identity: object) -> object:
            assert identity == JOB
            return type("Job", (), {"cancel_requested_at": None})()

    @asynccontextmanager
    async def scope() -> AsyncIterator[object]:
        yield Session()

    class Client:
        def __init__(self, _: object) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def load_image_worker(self, *_: object) -> ImageWorkerLoadResult:
            calls.append("load")
            return ImageWorkerLoadResult(
                instance_id=INSTANCE, state="starting", reserved_bytes=1024
            )

        async def image_worker_status(self, *_: object) -> ImageWorkerLoadResult:
            calls.append("ready")
            return ImageWorkerLoadResult(
                instance_id=INSTANCE,
                state="ready",
                reserved_bytes=1024,
                pid=123,
                process_create_time=100.0,
                port=9600,
            )

        async def reserve_image_inputs(
            self, node: str, command: NodeImageStartRequest
        ) -> NodeImageJob:
            assert node == NODE and command == prepared.start
            calls.append("reserve")
            return NodeImageJob(
                job_id=JOB,
                attempt=1,
                fence=1,
                node=NODE,
                instance_id=INSTANCE,
                state="queued",
                updated_at=datetime.now(UTC),
            )

        async def stage_image_input(
            self, node: str, command: NodeImageInputRequest, path: Path
        ) -> object:
            assert node == NODE and path == source
            assert await asyncio.to_thread(path.read_bytes) == payload
            assert command.sha256 == digest
            calls.append("stage")
            return object()

        async def start_image_job(self, node: str, command: NodeImageStartRequest) -> NodeImageJob:
            assert node == NODE and command == prepared.start
            calls.append("start")
            return NodeImageJob(
                job_id=JOB,
                attempt=1,
                fence=1,
                node=NODE,
                instance_id=INSTANCE,
                state="reserving",
                updated_at=datetime.now(UTC),
            )

    monkeypatch.setattr(images, "prepare_image_dispatch", prepare)
    monkeypatch.setattr(images, "NodeClient", Client)
    monkeypatch.setattr(images, "session_scope", scope)
    monkeypatch.setattr(
        images,
        "get_settings",
        lambda: Settings(  # type: ignore[call-arg]
            _secrets_dir="/nonexistent", image_input_derived_root=str(tmp_path)
        ),
    )
    assert await images.drive_image_dispatch(JOB)
    assert calls == ["reserve", "load", "ready", "stage", "start"]
