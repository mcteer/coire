"""Real CPU rank files + authenticated ASGI fabric; no Studio/MLX execution."""

import asyncio
import hashlib
import json
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
from fastapi import FastAPI, HTTPException, Request
from safetensors.numpy import load_file, save_file
from test_training_lifecycle import FakeProcesses, envelope, prepare_command, start

from coire_core.errors import TrainingConflict, TrainingValidationError
from coire_core.models.datasets import TokenizedTrainingExample
from coire_core.models.training_node import (
    CheckpointWorkerState,
    NodeRankCheckpointPayload,
    NodeTrainingEvent,
    TrainingArtifactGrantRefresh,
    TrainingPrepareRequest,
    TrainingRankCollection,
    TrainingRankGrantRequest,
    TrainingRankImportRequest,
)
from coire_core.net import DataFabricClient
from coire_core.settings import Settings
from coire_node.reservations import ReservationLedger
from coire_node.store import Store
from coire_node.training.checkpoints import CheckpointStore
from coire_node.training.components import RankImporter
from coire_node.training.distributed import PrivateRankTransport
from coire_node.training.journal import TrainingJournal
from coire_node.training.sampler import SingleSourceSampler
from coire_node.training.supervisor import TrainingSupervisor
from coire_node.training.worker import resolved_digest


class CpuIO:
    def is_tensor(self, value: object) -> bool:
        return isinstance(value, np.ndarray)

    def shape(self, value: Any) -> list[int]:
        return list(value.shape)

    def dtype(self, value: Any) -> str:
        return str(value.dtype)

    def save(self, path: Path, values: dict[str, Any]) -> None:
        save_file(values, str(path))

    def load(self, path: Path) -> dict[str, Any]:
        return dict(load_file(str(path)))


class Pair:
    def __init__(self, root: Path) -> None:
        value = prepare_command().model_dump(mode="json")
        value["world_size"] = 2
        value["resolved"]["spec"]["placement"]["mode"] = "data_parallel"
        value["resolved"]["spec"]["optim"]["batch_size"] = 2
        value["resolved"]["resource_envelope"]["checkpoint_bytes"] = 100_000
        hostfile = root / "hostfile.json"
        hostfile.write_text(
            json.dumps(
                {
                    "backend": "jaccl",
                    "hosts": [
                        {"ssh": "coire-edge-a.fabric", "rdma": [None, "rdma_en2"]},
                        {"ssh": "coire-edge-b.fabric", "rdma": ["rdma_en3", None]},
                    ],
                }
            )
        )
        value["collective"] = {
            "hostfile_sha256": hashlib.sha256(hostfile.read_bytes()).hexdigest(),
            "coordinator_port": 32323,
            "runtime_sha256": value["resolved"]["runtime_sha256"],
        }
        self.commands = [
            TrainingPrepareRequest.model_validate(
                {
                    **value,
                    "rank": rank,
                    "node": ("coire-edge-a", "coire-edge-b")[rank],
                    "command_id": str(uuid.uuid4()),
                }
            )
            for rank in (0, 1)
        ]
        self.journals = [
            TrainingJournal(root / f"journal-{rank}", node=c.node, admission_lock=threading.RLock())
            for rank, c in enumerate(self.commands)
        ]
        self.processes = [FakeProcesses(), FakeProcesses()]
        self.supervisors = [
            TrainingSupervisor(
                j,
                interpreter=Path("/opt/coire/envs/v1/bin/python"),
                validate_ready=lambda c: None,
                processes=p,
                jaccl_hostfile=hostfile,
            )
            for j, p in zip(self.journals, self.processes, strict=True)
        ]
        self.components = [s.components for s in self.supervisors]
        self.stores = [
            CheckpointStore(c.root, tensor_io=CpuIO(), disk_floor_bytes=0) for c in self.components
        ]
        self.identity = uuid.uuid4()
        self.manifests = []
        for rank, (journal, command, store, components) in enumerate(
            zip(self.journals, self.commands, self.stores, self.components, strict=True)
        ):
            journal.prepare(command, memory_available=10**6, disk_available=10**7, disk_floor=0)
            with journal.transaction():
                record = journal.get(command.attempt_id)
                record.update(liveness="running", spawn_nonce=str(uuid.uuid4()), update=3)
                journal.save(record)
            sampler = SingleSourceSampler(
                [
                    TokenizedTrainingExample(
                        source_row=i + 1,
                        content_sha256="a" * 64,
                        tokens=[1, 2, 3],
                        target_mask=[False, False, True],
                        target_start=2,
                    )
                    for i in range(4)
                ],
                dataset_sha256="b" * 64,
                batch_size=2,
                seed=0,
                max_sequence_length=8,
            )
            sampler.next_batch()
            state = CheckpointWorkerState(
                job_id=command.job_id,
                attempt_id=command.attempt_id,
                fence=command.fence,
                completed_update=3,
                rank=rank,
                world_size=2,
                runtime_sha256=command.resolved.runtime_sha256,
                resolved_spec_sha256=resolved_digest(command),
                optimizer=command.resolved.spec.optim,
                mlx_rng_key=(123, 456 + rank),
                sampler=sampler.snapshot(),
            )
            component = store.save_rank(
                state,
                {"lora_a": np.ones((2, 2), dtype=np.float32)},
                {"step": np.array(3, dtype=np.uint32), "m": np.ones(2, dtype=np.float32)},
                artifact_id=self.identity,
            )
            self.manifests.append(components.register(self.identity, component))

    def grant(self, source: int = 0) -> tuple[TrainingRankImportRequest, str]:
        manifest = self.manifests[source]
        scope = TrainingRankGrantRequest(
            command_id=uuid.uuid4(),
            artifact_id=self.identity,
            manifest_sha256=manifest.canonical_sha256(),
            source_node=self.commands[source].node,
            destination_node=self.commands[1 - source].node,
            attempt_id=manifest.attempt_id,
            fence=manifest.fence,
            file_ids=[f.id for f in manifest.files],
            max_bytes=manifest.total_bytes,
            expires_at=datetime.now(UTC) + timedelta(seconds=20),
            component=manifest,
        )
        issued = self.components[source].issue(scope)
        return TrainingRankImportRequest(
            command_id=uuid.uuid4(),
            artifact_id=self.identity,
            manifest_sha256=manifest.canonical_sha256(),
            source_node=scope.source_node,
            destination_node=scope.destination_node,
            attempt_id=scope.attempt_id,
            fence=scope.fence,
            grant_id=issued.grant_id,
            grant_secret=issued.secret,
            component=manifest,
        ), issued.secret

    def close(self) -> None:
        for journal in self.journals:
            journal.close()


@pytest.fixture
def pair(tmp_path: Path) -> Any:
    value = Pair(tmp_path)
    yield value
    value.close()


def test_component_grants_scope_and_revocation(pair: Pair) -> None:
    request, secret = pair.grant()
    manager = pair.components[0]
    manager.peer_addresses = lambda peer: {"192.0.2.2"}
    assert manager.authorize(secret, pair.identity, 0, "192.0.2.2") == request.component
    assert manager.authorize(secret, pair.identity, 1, "192.0.2.2") is None
    assert manager.authorize(secret, pair.identity, 0, "192.0.2.3") is None
    assert manager.authorize(secret, pair.identity, 0, "192.0.2.2", "unknown") is None
    assert manager.authorize(secret, uuid.uuid4(), 0, "192.0.2.2") is None
    manager.revoke(request.grant_id)
    assert manager.authorize(secret, pair.identity, 0, "192.0.2.2") is None


def test_private_rank_publication_is_typed_and_wrong_rank_event_is_refused(pair: Pair) -> None:
    journal, prepared, components = pair.journals[0], pair.commands[0], pair.components[0]
    transport = PrivateRankTransport(
        prepared,
        components,
        journal,
        read=lambda name: b"",
        control=lambda: "continue",
        scope=lambda command: None,
    )
    transport.publish(pair.identity, components.verify(pair.manifests[0]), time.monotonic() + 1)
    event = journal.events(prepared.attempt_id).items[0]
    assert event.payload.kind == "checkpoint_rank_staged"
    assert event.payload.component == pair.manifests[0]
    wrong = NodeTrainingEvent(
        sequence=journal.next_sequence(prepared.attempt_id),
        job_id=prepared.job_id,
        attempt_id=prepared.attempt_id,
        fence=prepared.fence,
        update=3,
        recorded_at=datetime.now(UTC),
        payload=NodeRankCheckpointPayload(component=pair.manifests[1]),
    )
    with pytest.raises(TrainingConflict, match="owned event scope"):
        journal.append_event(wrong)
    assert len(journal.events(prepared.attempt_id).items) == 1


@pytest.mark.asyncio
async def test_real_data_only_import_and_collection_mailbox(pair: Pair, tmp_path: Path) -> None:
    from coire_node.routes.training_components import data_router

    for source in (0, 1):
        command, secret = pair.grant(source)
        source_store = pair.components[source]
        source_store.peer_addresses = lambda peer: {"192.0.2.2"}
        app = FastAPI()
        app.state.training_components = source_store
        app.include_router(data_router)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("192.0.2.2", 123)), trust_env=False
        )
        importer = RankImporter(
            pair.components[1 - source],
            tmp_path / f"imports-{source}",
            port=8092,
            client=DataFabricClient(client=client),
            disk_floor_bytes=0,
        )
        initial = await importer.start(command)
        assert initial.state == "staging"
        await importer.tasks[command.command_id]
        final = importer.status(command.command_id)
        assert final.state == "verified" and final.receipt is not None
        assert final.receipt.node == command.destination_node
        encoded = (importer.root / f"{command.command_id}.json").read_text()
        assert secret not in encoded and "grant_secret" not in encoded
        assert await importer.start(command) == final
        await importer.aclose()
        await client.aclose()
    for rank, supervisor in enumerate(pair.supervisors):
        collection = TrainingRankCollection.model_validate(
            {
                **envelope(pair.commands[rank]),
                "command_id": str(uuid.uuid4()),
                "artifact_id": str(pair.identity),
                "components": [m.model_dump(mode="json") for m in pair.manifests],
            }
        )
        await supervisor.publish_collection(collection)
        path = supervisor.directory(collection.attempt_id) / f"collection-{pair.identity}.json"

        def read_mailbox(name: str, path: Path = path) -> bytes:
            return (path.parent / name).read_bytes()

        transport = PrivateRankTransport(
            pair.commands[rank],
            supervisor.components,
            supervisor.journal,
            read=read_mailbox,
            control=lambda: "continue",
            scope=lambda c: None,
        )
        state = supervisor.components.verify(pair.manifests[rank]).state
        collected = transport.collect(pair.identity, state, time.monotonic() + 1)
        assert {c.state.rank for c in collected} == {0, 1}
        bundle = pair.stores[rank].publish_rank_bundle(collected, artifact_id=pair.identity)
        assert len(bundle.ranks) == 2


@pytest.mark.asyncio
async def test_missing_peer_and_cancelled_attempt_cannot_publish_collection(pair: Pair) -> None:
    supervisor, prepared = pair.supervisors[0], pair.commands[0]
    command = TrainingRankCollection.model_validate(
        {
            **envelope(prepared),
            "command_id": str(uuid.uuid4()),
            "artifact_id": str(pair.identity),
            "components": [m.model_dump(mode="json") for m in pair.manifests],
        }
    )
    with pytest.raises(TrainingValidationError):
        await supervisor.publish_collection(command)
    assert not (
        supervisor.directory(command.attempt_id) / f"collection-{pair.identity}.json"
    ).exists()
    with supervisor.journal.transaction():
        record = supervisor.journal.get(command.attempt_id)
        record["reason"] = "cancelled"
        supervisor.journal.save(record)
    with pytest.raises(TrainingConflict, match="authority"):
        pair.components[0].verify(pair.manifests[0])


@pytest.mark.asyncio
async def test_expired_grant_refresh_and_range_retry(pair: Pair, tmp_path: Path) -> None:
    request, secret = pair.grant()
    seen: list[str] = []
    ranges: list[str | None] = []
    allow = [False]

    def respond(http: httpx.Request) -> httpx.Response:
        assert http.url.host == "coire-edge-a.fabric"
        seen.append(str(http.url))
        ranges.append(http.headers.get("range"))
        if not allow[0]:
            return httpx.Response(403)
        file = next(f for f in request.component.files if f.id == http.url.path.rsplit("/", 1)[-1])
        encoded = (pair.components[0].directory(pair.identity, 0) / file.name).read_bytes()
        offset = int(http.headers.get("range", "bytes=0-")[6:-1])
        return httpx.Response(
            206 if offset else 200,
            content=encoded[offset:],
            headers={"Content-Range": f"bytes {offset}-{len(encoded) - 1}/{len(encoded)}"}
            if offset
            else {},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    importer = RankImporter(
        pair.components[1],
        tmp_path / "imports",
        port=8092,
        client=DataFabricClient(client=client),
        disk_floor_bytes=0,
    )
    await importer.start(request)
    await importer.tasks[request.command_id]
    assert importer.status(request.command_id).state == "failed"
    staging = pair.components[1].directory(pair.identity, 0).parent / ".import-rank-0"
    first = request.component.files[0]
    source = pair.components[0].directory(pair.identity, 0) / first.name
    (staging / first.name).write_bytes(source.read_bytes()[:10])
    (staging / first.name).chmod(0o600)
    allow[0] = True
    refresh = TrainingArtifactGrantRefresh(
        command_id=uuid.uuid4(),
        artifact_id=pair.identity,
        manifest_sha256=request.manifest_sha256,
        attempt_id=request.attempt_id,
        fence=request.fence,
        grant_id=uuid.uuid4(),
        grant_secret=secret,
    )
    await importer.refresh(request.command_id, refresh)
    await importer.tasks[request.command_id]
    assert importer.status(request.command_id).state == "verified"
    assert all(".fabric:" in url for url in seen)
    assert "bytes=10-" in ranges
    await importer.aclose()
    await client.aclose()


def test_old_component_grant_is_fenced_by_new_attempt(pair: Pair) -> None:
    request, secret = pair.grant()
    manager, journal = pair.components[0], pair.journals[0]
    manager.peer_addresses = lambda peer: {"192.0.2.2"}
    with journal.transaction():
        record = journal.get(request.attempt_id)
        record.update(liveness="stopped")
        journal.save(record)
    journal.release_after_death(request.attempt_id)
    value = pair.commands[0].model_dump(mode="json")
    value.update(attempt_id="01ARZ3NDEKTSV4RRFFQ69G5FAW", fence=2, command_id=str(uuid.uuid4()))
    journal.prepare(
        TrainingPrepareRequest.model_validate(value),
        memory_available=10**6,
        disk_available=10**7,
        disk_floor=0,
    )
    with pytest.raises(TrainingConflict, match="authority"):
        manager.authorize(secret, pair.identity, 0, "192.0.2.2")


@pytest.mark.asyncio
async def test_corrupt_component_import_cannot_publish_peer_or_common_bundle(
    pair: Pair, tmp_path: Path
) -> None:
    request, _ = pair.grant()

    def corrupt(http: httpx.Request) -> httpx.Response:
        file = next(f for f in request.component.files if f.id == http.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, content=b"x" * file.bytes)

    client = httpx.AsyncClient(transport=httpx.MockTransport(corrupt))
    importer = RankImporter(
        pair.components[1],
        tmp_path / "imports",
        port=8092,
        client=DataFabricClient(client=client),
        disk_floor_bytes=0,
    )
    await importer.start(request)
    await importer.tasks[request.command_id]
    assert importer.status(request.command_id).state == "failed"
    assert importer.status(request.command_id).receipt is None
    assert not pair.components[1].directory(pair.identity, 0).exists()
    assert not pair.stores[1].path_for(pair.identity).exists()
    # The owned rank remains intact; failure does not release its training hold.
    pair.components[1].verify(pair.manifests[1])
    assert not pair.journals[1].get(request.attempt_id)["released"]
    await importer.aclose()
    await client.aclose()


@pytest.mark.asyncio
async def test_import_deadline_and_restart_never_reanimate_transport(
    pair: Pair, tmp_path: Path
) -> None:
    request, secret = pair.grant()
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda http: httpx.Response(403)))
    importer = RankImporter(
        pair.components[1],
        tmp_path / "imports",
        port=8092,
        client=DataFabricClient(client=client),
        disk_floor_bytes=0,
    )
    await importer.start(request)
    await importer.tasks[request.command_id]
    importer.deadlines[request.command_id] = time.monotonic() - 1
    refresh = TrainingArtifactGrantRefresh(
        command_id=uuid.uuid4(),
        artifact_id=pair.identity,
        manifest_sha256=request.manifest_sha256,
        attempt_id=request.attempt_id,
        fence=request.fence,
        grant_id=uuid.uuid4(),
        grant_secret=secret,
    )
    with pytest.raises(TrainingConflict, match="resurrected"):
        await importer.refresh(request.command_id, refresh)
    restarted = RankImporter(pair.components[1], importer.root, port=8092, disk_floor_bytes=0)
    assert restarted.status(request.command_id).state == "failed"
    assert not restarted.tasks
    with pytest.raises(TrainingConflict, match="resurrected"):
        await restarted.refresh(request.command_id, refresh)
    await restarted.aclose()
    await importer.aclose()
    await client.aclose()


@pytest.mark.asyncio
async def test_import_cancel_and_deadline_cannot_resurrect(pair: Pair, tmp_path: Path) -> None:
    request, secret = pair.grant()
    entered = asyncio.Event()

    async def blocked(http: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError

    client = httpx.AsyncClient(transport=httpx.MockTransport(blocked))
    importer = RankImporter(
        pair.components[1],
        tmp_path / "imports",
        port=8092,
        client=DataFabricClient(client=client),
        disk_floor_bytes=0,
    )
    await importer.start(request)
    await entered.wait()
    assert (await importer.cancel(request.command_id)).state == "cancelled"
    with pytest.raises(TrainingConflict, match="resurrected"):
        await importer.refresh(
            request.command_id,
            TrainingArtifactGrantRefresh(
                command_id=uuid.uuid4(),
                artifact_id=pair.identity,
                manifest_sha256=request.manifest_sha256,
                attempt_id=request.attempt_id,
                fence=request.fence,
                grant_id=uuid.uuid4(),
                grant_secret=secret,
            ),
        )
    assert not pair.components[1].directory(pair.identity, 0).exists()
    assert not importer.holds
    await importer.aclose()
    await client.aclose()


@pytest.mark.asyncio
async def test_native_owned_rank_environment_and_half_spawn_hold(pair: Pair) -> None:
    binding = pair.commands[0].collective
    assert binding is not None
    changed = pair.commands[0].model_copy(
        update={"collective": binding.model_copy(update={"coordinator_port": 32324})}
    )
    with pytest.raises(TrainingValidationError, match="binding"):
        pair.supervisors[0].collective_launch(changed)
    for rank, supervisor in enumerate(pair.supervisors):
        prepared = pair.commands[rank]
        with supervisor.journal.transaction():
            record = supervisor.journal.get(prepared.attempt_id)
            record.update(liveness="prepared", spawn_nonce=None)
            supervisor.journal.save(record)
        if rank == 1:
            pair.processes[rank].half_spawn = True
            with pytest.raises(RuntimeError, match="before PID"):
                await supervisor.start(start(prepared))
            record = supervisor.journal.get(prepared.attempt_id)
            assert record["spawn_nonce"] is not None and not record["released"]
            assert record["liveness"] == "unknown"
        else:
            receipt = await supervisor.start(start(prepared))
            assert receipt.pid == 1234
        assert pair.processes[rank].env["MLX_RANK"] == str(rank)
        assert pair.processes[rank].env["MLX_JACCL_COORDINATOR"] == "coire-edge-a.fabric:32323"
        assert "ssh" not in pair.processes[rank].argv


@pytest.mark.asyncio
async def test_component_listener_auth_and_control_data_separation(
    pair: Pair, tmp_path: Path
) -> None:
    control, data = FastAPI(), FastAPI()

    async def auth(request: Request) -> None:
        if request.headers.get("authorization") != "Bearer test-token":
            raise HTTPException(401)

    control.state.require_node_token = auth
    importer = RankImporter(pair.components[0], tmp_path / "imports", port=8092, disk_floor_bytes=0)
    pair.components[0].attach(control, data, importer=importer)
    path = f"/node/training/components/{pair.identity}/ranks/0"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://control"
    ) as client:
        assert (await client.get(path)).status_code == 401
        assert (
            await client.get(path, headers={"Authorization": "Bearer test-token"})
        ).status_code == 200
        assert (
            await client.get(f"/training-components/{pair.identity}/ranks/0/manifest")
        ).status_code == 404
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=data), base_url="http://data"
    ) as client:
        assert (await client.get(path)).status_code == 404
        assert (
            await client.get(f"/training-components/{pair.identity}/ranks/0/manifest")
        ).status_code == 404
    await importer.aclose()


@pytest.mark.asyncio
async def test_component_transfer_holds_real_ledger_until_cancel_is_drained(
    pair: Pair, tmp_path: Path
) -> None:
    request, _ = pair.grant()
    ledger = ReservationLedger(
        Settings(node_state_dir=str(tmp_path / "ledger-state")),
        Store(tmp_path / "models"),
        lambda: 0,
    )
    entered = asyncio.Event()

    async def blocked(http: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError

    client = httpx.AsyncClient(transport=httpx.MockTransport(blocked))
    importer = RankImporter(
        pair.components[1],
        tmp_path / "imports",
        port=8092,
        client=DataFabricClient(client=client),
        disk_floor_bytes=0,
        reservations=ledger,
    )
    await importer.start(request)
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert ledger.held_bytes() == 192 * 1024**2
    assert ledger.held_disk_bytes(pair.components[1].root) == request.component.total_bytes
    await importer.cancel(request.command_id)
    assert ledger.held_bytes() == 0 and ledger.held_disk_bytes(pair.components[1].root) == 0
    assert importer.status(request.command_id).state == "cancelled"
    await importer.aclose()
    await client.aclose()
