"""Real Postgres and authenticated typed simulated-node coordinator/recovery tests."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_training_runtime_postgres import RuntimeDatabase
from test_training_runtime_postgres import runtime_db as runtime_db
from training_measurement_fixtures import ATTEMPT, DIGEST, JOB

from coire_api.db import (
    MemoryReservationRow,
    NodeRow,
    TrainingAttemptRow,
    TrainingCommandRow,
    TrainingJobRow,
    TrainingMetricRow,
    TrainingParticipantRow,
)
from coire_api.training.service import payload_digest
from coire_api.training_executor import (
    TrainingNodeClient,
    mirror_checkpoint,
)
from coire_core.errors import TrainingConflict
from coire_core.models.training import ResolvedTrainingSpec, TrainingMetricSample
from coire_core.models.training_node import (
    NodeProgressPayload,
    NodeTrainingEvent,
    NodeTrainingStatus,
    TrainingArtifactGrantIssued,
    TrainingArtifactManifest,
    TrainingArtifactVerificationReceipt,
    TrainingArtifactVerifyRequest,
    TrainingPrepareRequest,
    TrainingRankCollection,
    TrainingRankComponentManifest,
    TrainingRankGrantRequest,
    TrainingRankImportRequest,
    TrainingRankImportStatus,
    TrainingRankVerificationReceipt,
)
from coire_core.settings import Settings
from coire_scheduler.training import ingest_training_event
from coire_scheduler.training_components import (
    COLLECTION,
    COMPONENT,
    IMPORT,
    TrainingComponentCoordinator,
    bind_collective_prepares,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1", reason="requires disposable PostgreSQL"
    ),
]


@pytest.fixture
async def rank_db(runtime_db: RuntimeDatabase) -> AsyncIterator[RuntimeDatabase]:
    factory, prepare = runtime_db
    data = prepare.resolved.model_dump(mode="json")
    data["spec"]["placement"] = {"mode": "data_parallel"}
    data["spec"]["optim"]["batch_size"] = 2
    data["spec"]["data"]["train"]["epoch_samples"] = 2
    data["spec"]["data"]["train"]["replacement"] = True
    resolved = ResolvedTrainingSpec.model_validate(data)
    prepare = prepare.model_copy(
        update={
            "resolved": resolved,
            "world_size": 2,
            "request_sha256": payload_digest(resolved),
            "lease_expires_at": datetime.now(UTC) + timedelta(minutes=5),
        }
    )
    async with factory.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        attempt = await session.get(TrainingAttemptRow, ATTEMPT)
        participant = await session.scalar(select(TrainingParticipantRow))
        command = await session.get(TrainingCommandRow, prepare.command_id)
        assert (
            job is not None
            and attempt is not None
            and participant is not None
            and command is not None
        )
        job.state, job.resolved_spec, job.resolved_sha256 = (
            "running",
            resolved.model_dump(mode="json"),
            payload_digest(resolved),
        )
        attempt.state, attempt.world_size, attempt.lease_expires_at = (
            "running",
            2,
            prepare.lease_expires_at,
        )
        participant.request_sha256 = prepare.request_sha256
        command.payload, command.request_sha256 = (
            prepare.model_dump(mode="json"),
            payload_digest(prepare),
        )
        node = await session.scalar(select(NodeRow).where(NodeRow.name == "coire-edge-b"))
        assert node is not None
        reservation, disk, identity = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        session.add(
            MemoryReservationRow(
                id=reservation,
                node_id=node.id,
                holder_type="training",
                holder_id=ATTEMPT,
                bytes=resolved.resource_envelope.memory_bytes,
                pinned=True,
                state="held",
            )
        )
        await session.flush()
        session.add(
            TrainingParticipantRow(
                attempt_id=ATTEMPT,
                node_id=node.id,
                rank=1,
                reservation_id=reservation,
                disk_reservation_id=disk,
                command_id=identity,
                request_sha256=prepare.request_sha256,
                spawn_nonce=uuid.uuid4(),
            )
        )
        other = prepare.model_copy(
            update={
                "command_id": identity,
                "node": "coire-edge-b",
                "rank": 1,
                "reservation_id": reservation,
                "disk_reservation_id": disk,
            }
        )
        session.add(
            TrainingCommandRow(
                id=identity,
                actor_user_id=job.owner_user_id,
                idempotency_key="prepare-rank-1",
                operation="node.training.prepare",
                subject_id="coire-edge-b",
                job_id=JOB,
                attempt_id=ATTEMPT,
                request_sha256=payload_digest(other),
                payload=other.model_dump(mode="json"),
                state="pending",
            )
        )
    yield factory, prepare


def rank_component(
    prepare: TrainingPrepareRequest, rank: int, artifact: uuid.UUID, update: int = 1
) -> TrainingRankComponentManifest:
    files = [
        {
            "id": f"rank{rank}-{name}",
            "name": f"rank{rank}-{name}{suffix}",
            "bytes": 10,
            "sha256": DIGEST,
        }
        for name, suffix in (
            ("adapter", ".safetensors"),
            ("optimizer", ".safetensors"),
            ("state", ".json"),
        )
    ]
    return TrainingRankComponentManifest.model_validate(
        {
            "artifact_id": artifact,
            "job_id": JOB,
            "attempt_id": ATTEMPT,
            "fence": 1,
            "update": update,
            "rank": rank,
            "runtime_sha256": DIGEST,
            "resolved_spec_sha256": payload_digest(prepare.resolved),
            "files": files,
            "total_bytes": 30,
            "state": {
                "rank": rank,
                "update": update,
                "adapter_file_id": files[0]["id"],
                "optimizer_file_id": files[1]["id"],
                "state_file_id": files[2]["id"],
                "adapter_tensors": [{"key": "a", "shape": [1], "dtype": "float32"}],
                "optimizer_tensors": [{"key": "m", "shape": [1], "dtype": "float32"}],
            },
        }
    )


def rank_event(component: TrainingRankComponentManifest, sequence: int = 1) -> NodeTrainingEvent:
    return NodeTrainingEvent.model_validate(
        {
            "sequence": sequence,
            "job_id": JOB,
            "attempt_id": ATTEMPT,
            "fence": component.fence,
            "update": component.update,
            "recorded_at": datetime.now(UTC),
            "payload": {"kind": "checkpoint_rank_staged", "component": component},
        }
    )


class AuthenticatedFabric:
    """Real typed client plumbing over a simulated authenticated control fabric only."""

    def __init__(self, components: list[TrainingRankComponentManifest]) -> None:
        self.local = {
            ("coire-edge-a" if c.rank == 0 else "coire-edge-b", c.rank): c for c in components
        }
        self.imports: dict[uuid.UUID, TrainingRankImportStatus] = {}
        self.grants: dict[uuid.UUID, tuple[TrainingRankGrantRequest, str]] = {}
        self.collections: dict[str, TrainingRankCollection] = {}
        self.pending = False
        self.calls: list[tuple[str, str, str]] = []
        self.manifest: TrainingArtifactManifest | None = None
        self.cancelled: list[uuid.UUID] = []
        self.lose_collection_ack = False

    async def request(self, method: str, node: str, path: str, **kwargs: Any) -> httpx.Response:
        assert kwargs["headers"]["Authorization"] == f"Bearer test-token-{node}"
        self.calls.append((method, node, path))
        body = kwargs.get("json")
        if "/ranks/" in path:
            rank = int(path.split("/ranks/")[1].split("/")[0])
            component = self.local[(node, rank)]
            if method == "GET":
                return httpx.Response(200, json=component.model_dump(mode="json"))
            verification = TrainingArtifactVerifyRequest.model_validate(body)
            assert verification.manifest_sha256 == component.canonical_sha256()
            return httpx.Response(
                200,
                json=TrainingRankVerificationReceipt(
                    command_id=verification.command_id,
                    component=component,
                    node=node,
                    verified_bytes=component.total_bytes,
                ).model_dump(mode="json"),
            )
        if path == "/node/training/components/grants":
            grant = TrainingRankGrantRequest.model_validate(body)
            assert (
                grant.source_node == node
                and self.local[(node, grant.component.rank)] == grant.component
            )
            identity, secret = uuid.uuid4(), "component-test-secret-" + uuid.uuid4().hex
            self.grants[identity] = (grant, secret)
            return httpx.Response(
                200,
                json=TrainingArtifactGrantIssued(
                    grant_id=identity, secret=secret, expires_at=grant.expires_at
                ).model_dump(mode="json"),
            )
        if path == "/node/training/components/imports":
            command = TrainingRankImportRequest.model_validate(body)
            grant, secret = self.grants[command.grant_id]
            assert command.grant_secret == secret and grant.destination_node == node
            assert command.component == grant.component and command.source_node == grant.source_node
            status = TrainingRankImportStatus(
                import_id=command.command_id,
                component=command.component,
                state="transferring",
                transferred_bytes=0,
            )
            self.imports[command.command_id] = status
            if not self.pending:
                status = self.finish(node, status)
            return httpx.Response(202, json=status.model_dump(mode="json"))
        if path.startswith("/node/training/components/imports/"):
            identity = uuid.UUID(path.split("/imports/")[1].split("/")[0])
            if identity not in self.imports:
                return httpx.Response(404, json={})
            status = self.imports[identity]
            if path.endswith("/cancel"):
                self.cancelled.append(identity)
                status = status.model_copy(update={"state": "cancelled", "reason": "cancelled"})
                self.imports[identity] = status
            elif not self.pending:
                status = self.finish(node, status)
            return httpx.Response(200, json=status.model_dump(mode="json"))
        if path.endswith("/rank-collection"):
            collection = TrainingRankCollection.model_validate(body)
            assert collection.node == node
            assert all(self.local[(node, c.rank)] == c for c in collection.components)
            self.collections[node] = collection
            if self.lose_collection_ack:
                self.lose_collection_ack = False
                raise httpx.ReadTimeout("synthetic lost control acknowledgement")
            return httpx.Response(
                200,
                json=NodeTrainingStatus(
                    job_id=JOB,
                    attempt_id=ATTEMPT,
                    fence=1,
                    node=node,
                    liveness="running",
                    update=collection.components[0].update,
                    lease_expires_at=collection.lease_expires_at,
                ).model_dump(mode="json"),
            )
        if path.endswith("/verify") and "/artifacts/" in path:
            assert self.manifest is not None
            verify = TrainingArtifactVerifyRequest.model_validate(body)
            assert verify.manifest_sha256 == self.manifest.canonical_sha256()
            return httpx.Response(
                200,
                json=TrainingArtifactVerificationReceipt(
                    command_id=verify.command_id,
                    artifact_id=self.manifest.artifact_id,
                    node=node,
                    manifest_sha256=self.manifest.canonical_sha256(),
                    verified_bytes=self.manifest.total_bytes,
                ).model_dump(mode="json"),
            )
        if "/artifacts/imports/" in path:
            from coire_core.models.training_node import TrainingArtifactImportStatus

            assert self.manifest is not None
            identity = uuid.UUID(path.split("/imports/")[1])
            return httpx.Response(
                200,
                json=TrainingArtifactImportStatus(
                    import_id=identity,
                    artifact_id=self.manifest.artifact_id,
                    manifest_sha256=self.manifest.canonical_sha256(),
                    state="verified",
                    transferred_bytes=self.manifest.total_bytes,
                    verified_manifest=self.manifest,
                ).model_dump(mode="json"),
            )
        raise AssertionError((method, node, path))

    def finish(self, node: str, status: TrainingRankImportStatus) -> TrainingRankImportStatus:
        self.local[(node, status.component.rank)] = status.component
        status = status.model_copy(
            update={
                "state": "verified",
                "transferred_bytes": status.component.total_bytes,
                "receipt": TrainingRankVerificationReceipt(
                    command_id=status.import_id,
                    component=status.component,
                    node=node,
                    verified_bytes=status.component.total_bytes,
                ),
            }
        )
        self.imports[status.import_id] = status
        return status


def client_for(fabric: AuthenticatedFabric, monkeypatch: pytest.MonkeyPatch) -> TrainingNodeClient:
    settings = Settings(
        training_enabled=True,
        node_tokens=SecretStr(
            json.dumps({node: f"test-token-{node}" for node in ("coire-edge-a", "coire-edge-b")})
        ),
    )
    client = TrainingNodeClient(settings, timeout=5)
    monkeypatch.setattr(client._control, "request", fabric.request)
    return client


async def stage_ranks(
    database: RuntimeDatabase, components: list[TrainingRankComponentManifest]
) -> None:
    factory, _ = database
    for component in components:
        async with factory.begin() as session:
            await ingest_training_event(
                session,
                "coire-edge-a" if component.rank == 0 else "coire-edge-b",
                rank_event(component),
            )


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
async def test_rank_progress_keeps_both_mailboxes_and_one_authoritative_loss(
    rank_db: RuntimeDatabase, order: tuple[int, int]
) -> None:
    factory, prepare = rank_db
    events = {}
    for rank in order:
        metric = TrainingMetricSample(
            job_id=JOB,
            attempt_id=ATTEMPT,
            update=1,
            kind="train",
            loss=0.5,
            learning_rate=0.001,
            tokens=8,
            tokens_per_second=10 + rank,
            updates_per_second=2 + rank,
            footprint_bytes=100 + rank,
            peak_bytes=200 + rank,
            recorded_at=datetime.now(UTC),
        )
        events[rank] = NodeTrainingEvent(
            sequence=1,
            job_id=JOB,
            attempt_id=ATTEMPT,
            fence=prepare.fence,
            update=1,
            recorded_at=metric.recorded_at,
            payload=NodeProgressPayload(metric=metric),
        )
        async with factory.begin() as session:
            await ingest_training_event(
                session, ("coire-edge-a", "coire-edge-b")[rank], events[rank]
            )
    artifact = uuid.uuid4()
    for rank in order:
        async with factory.begin() as session:
            await ingest_training_event(
                session,
                ("coire-edge-a", "coire-edge-b")[rank],
                rank_event(rank_component(prepare, rank, artifact), sequence=2),
            )
            await ingest_training_event(
                session, ("coire-edge-a", "coire-edge-b")[rank], events[rank]
            )
    async with factory.begin() as session:
        metrics = list((await session.scalars(select(TrainingMetricRow))).all())
        assert len(metrics) == 1
        primary = events[0].payload
        assert primary.kind == "progress"
        assert metrics[0].metric == primary.metric.model_dump(mode="json")
        receipts = list(
            (
                await session.scalars(
                    select(TrainingCommandRow).where(
                        TrainingCommandRow.operation == "node.training.event"
                    )
                )
            ).all()
        )
        assert len(receipts) == 4
        components = list(
            (
                await session.scalars(
                    select(TrainingCommandRow).where(TrainingCommandRow.operation == COMPONENT)
                )
            ).all()
        )
        assert len(components) == 2


async def test_both_orientations_verified_collection_then_both_full_copies_commit(
    rank_db: RuntimeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, prepare = rank_db
    artifact = uuid.uuid4()
    components = [rank_component(prepare, rank, artifact) for rank in (0, 1)]
    await stage_ranks(rank_db, components)
    fabric = AuthenticatedFabric(components)
    client = client_for(fabric, monkeypatch)
    try:
        await TrainingComponentCoordinator(client._settings, client).run_once()
        assert set(fabric.collections) == {"coire-edge-a", "coire-edge-b"}
        async with factory.begin() as session:
            commands = list(await session.scalars(select(TrainingCommandRow)))
            assert sum(c.operation == COMPONENT for c in commands) == 2
            assert sum(c.operation == COLLECTION and c.state == "succeeded" for c in commands) == 2
            assert sum(c.operation == IMPORT and c.state == "succeeded" for c in commands) == 2
            assert all(
                secret not in str(c.payload) + str(c.receipt)
                for _, secret in fabric.grants.values()
                for c in commands
            )
            assert not any(c.operation == "node.training.checkpoint-commit" for c in commands)
        manifest = TrainingArtifactManifest(
            artifact_id=artifact,
            kind="checkpoint",
            files=[f for c in components for f in c.files],
            total_bytes=60,
            job_id=JOB,
            attempt_id=ATTEMPT,
            fence=1,
            update=1,
            world_size=2,
            runtime_sha256=DIGEST,
            resolved_spec_sha256=payload_digest(prepare.resolved),
            ranks=[c.state for c in components],
        )
        fabric.manifest = manifest
        async with factory.begin() as session:
            await ingest_training_event(
                session,
                "coire-edge-a",
                NodeTrainingEvent.model_validate(
                    {
                        "sequence": 2,
                        "job_id": JOB,
                        "attempt_id": ATTEMPT,
                        "fence": 1,
                        "update": 1,
                        "recorded_at": datetime.now(UTC),
                        "payload": {"kind": "checkpoint_staged", "manifest": manifest},
                    }
                ),
            )
        assert not await mirror_checkpoint(artifact, client)
        async with factory.begin() as session:
            job = await session.get(TrainingJobRow, JOB)
            assert job is not None and job.latest_checkpoint_id is None
            await ingest_training_event(
                session,
                "coire-edge-b",
                NodeTrainingEvent.model_validate(
                    {
                        "sequence": 2,
                        "job_id": JOB,
                        "attempt_id": ATTEMPT,
                        "fence": 1,
                        "update": 1,
                        "recorded_at": datetime.now(UTC),
                        "payload": {"kind": "checkpoint_staged", "manifest": manifest},
                    }
                ),
            )
        assert await mirror_checkpoint(artifact, client)
        async with factory.begin() as session:
            job = await session.get(TrainingJobRow, JOB)
            assert job is not None and job.latest_checkpoint_id == artifact
            acknowledgements = list(
                await session.scalars(
                    select(TrainingCommandRow).where(
                        TrainingCommandRow.operation == "node.training.checkpoint-commit"
                    )
                )
            )
            assert {row.subject_id for row in acknowledgements} == {"coire-edge-a", "coire-edge-b"}
    finally:
        await client.aclose()


async def test_restart_pending_transfer_reuses_intents_and_never_collects_early(
    rank_db: RuntimeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, prepare = rank_db
    components = [
        rank_component(prepare, rank, uuid.uuid5(uuid.NAMESPACE_URL, ATTEMPT)) for rank in (0, 1)
    ]
    await stage_ranks(rank_db, components)
    fabric = AuthenticatedFabric(components)
    fabric.pending = True
    client = client_for(fabric, monkeypatch)
    try:
        await TrainingComponentCoordinator(client._settings, client).run_once()
        assert not fabric.collections
        imports = set(fabric.imports)
        fabric.pending = False
        await TrainingComponentCoordinator(client._settings, client).run_once()
        assert set(fabric.imports) == imports and len(fabric.grants) == 2
        assert len(fabric.collections) == 2
        async with factory.begin() as session:
            assert (
                len(
                    list(
                        await session.scalars(
                            select(TrainingCommandRow).where(TrainingCommandRow.operation == IMPORT)
                        )
                    )
                )
                == 2
            )
    finally:
        await client.aclose()


async def test_lost_collection_ack_replays_exact_command_after_restart(
    rank_db: RuntimeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, prepare = rank_db
    components = [
        rank_component(prepare, rank, uuid.uuid5(uuid.NAMESPACE_URL, ATTEMPT)) for rank in (0, 1)
    ]
    await stage_ranks(rank_db, components)
    fabric = AuthenticatedFabric(components)
    fabric.lose_collection_ack = True
    client = client_for(fabric, monkeypatch)
    try:
        await TrainingComponentCoordinator(client._settings, client).run_once()
        first = fabric.collections["coire-edge-a"]
        async with factory.begin() as session:
            pending = await session.get(TrainingCommandRow, first.command_id)
            assert (
                pending is not None and pending.state == "dispatching" and pending.receipt is None
            )
        await TrainingComponentCoordinator(client._settings, client).run_once()
        assert fabric.collections["coire-edge-a"] == first
        assert len(fabric.collections) == 2 and len(fabric.grants) == 2
        async with factory.begin() as session:
            rows = list(
                await session.scalars(
                    select(TrainingCommandRow).where(TrainingCommandRow.operation == COLLECTION)
                )
            )
            assert len(rows) == 2 and all(row.state == "succeeded" for row in rows)
    finally:
        await client.aclose()


@pytest.mark.parametrize("mutation", ["node", "runtime", "resolved", "fence", "update"])
async def test_rank_events_refuse_wrong_current_identity(
    rank_db: RuntimeDatabase, mutation: str
) -> None:
    factory, prepare = rank_db
    component = rank_component(prepare, 0, uuid.uuid4())
    if mutation != "node":
        mutations: dict[str, dict[str, Any]] = {
            "runtime": {"runtime_sha256": "b" * 64},
            "resolved": {"resolved_spec_sha256": "b" * 64},
            "fence": {"fence": 2},
            "update": {"update": 3, "state": component.state.model_copy(update={"update": 3})},
        }
        component = component.model_copy(update=mutations[mutation])
    async with factory.begin() as session:
        with pytest.raises(TrainingConflict):
            await ingest_training_event(
                session,
                "coire-edge-b" if mutation == "node" else "coire-edge-a",
                rank_event(component),
            )


async def test_rank_update_cannot_rewind_or_replace_accepted_descriptor(
    rank_db: RuntimeDatabase,
) -> None:
    factory, prepare = rank_db
    component = rank_component(prepare, 0, uuid.uuid4())
    await stage_ranks(rank_db, [component])
    # An exact duplicate at another mailbox sequence advances only the cursor.
    async with factory.begin() as session:
        await ingest_training_event(session, "coire-edge-a", rank_event(component, sequence=2))
    rewound = rank_component(prepare, 0, uuid.uuid4(), update=0)
    async with factory.begin() as session:
        with pytest.raises(TrainingConflict, match="monotonically"):
            await ingest_training_event(session, "coire-edge-a", rank_event(rewound, sequence=3))
    changed = component.model_copy(
        update={
            "files": [
                component.files[0].model_copy(update={"sha256": "b" * 64}),
                *component.files[1:],
            ]
        }
    )
    async with factory.begin() as session:
        with pytest.raises(TrainingConflict, match="immutable"):
            await ingest_training_event(session, "coire-edge-a", rank_event(changed, sequence=3))
        rows = list(
            await session.scalars(
                select(TrainingCommandRow).where(TrainingCommandRow.operation == COMPONENT)
            )
        )
        assert len(rows) == 1 and rows[0].payload == component.model_dump(mode="json")


async def test_cancel_revokes_dispatch_and_stops_both_with_holds_counted(
    rank_db: RuntimeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory, prepare = rank_db
    components = [
        rank_component(prepare, rank, uuid.uuid5(uuid.NAMESPACE_URL, ATTEMPT)) for rank in (0, 1)
    ]
    await stage_ranks(rank_db, components)
    fabric = AuthenticatedFabric(components)
    fabric.pending = True
    client = client_for(fabric, monkeypatch)
    try:
        coordinator = TrainingComponentCoordinator(client._settings, client)
        await coordinator.run_once()
        async with factory.begin() as session:
            job = await session.get(TrainingJobRow, JOB, with_for_update=True)
            assert job is not None
            job.state = "cancelling"
        await coordinator.run_once()
        assert not fabric.collections and set(fabric.cancelled) == set(fabric.imports)
        async with factory.begin() as session:
            stops = list(
                await session.scalars(
                    select(TrainingCommandRow).where(
                        TrainingCommandRow.operation == "node.training.stop"
                    )
                )
            )
            assert {s.subject_id for s in stops} == {"coire-edge-a", "coire-edge-b"}
            holds = list(await session.scalars(select(MemoryReservationRow)))
            assert len(holds) == 2 and all(
                h.state.value in {"pending", "held", "releasing"} for h in holds
            )
    finally:
        await client.aclose()


async def test_collective_binding_requires_measured_link_and_declared_generated_file(
    rank_db: RuntimeDatabase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from coire_core.models import LinkState, RdmaState, StudioLinkProjection

    factory, _prepare = rank_db
    async with factory.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        attempt = await session.get(TrainingAttemptRow, ATTEMPT)
        assert job is not None and attempt is not None
        job.state, attempt.state = "reserving", "preparing"
        for node in await session.scalars(select(NodeRow)):
            node.data_host = node.name + ".fabric"
    async with factory.begin() as session:
        with pytest.raises(TrainingConflict, match="measured"):
            await bind_collective_prepares(session, ATTEMPT, Settings(training_enabled=True))

    async def link(session: AsyncSession, settings: Settings) -> StudioLinkProjection:
        return StudioLinkProjection(
            node_a="coire-edge-a",
            node_b="coire-edge-b",
            ip_state=LinkState.UP,
            rdma_state=RdmaState.UP,
            fallback_state=LinkState.DOWN,
            tp_eligible=True,
            required_after=datetime.now(UTC) - timedelta(seconds=60),
        )

    monkeypatch.setattr("coire_api.sharding.link_projection", link)
    async with factory.begin() as session:
        with pytest.raises(TrainingConflict, match="hostfile is unavailable"):
            await bind_collective_prepares(
                session,
                ATTEMPT,
                Settings(
                    training_enabled=True,
                    sharding_jaccl_hostfile=str(tmp_path / "missing-generated.json"),
                ),
            )
    path = tmp_path / "generated.json"
    path.write_text(
        json.dumps(
            {
                "backend": "jaccl",
                "hosts": [
                    {"ssh": "coire-edge-a.fabric", "rdma": [None, "fixture-rdma"]},
                    {"ssh": "coire-edge-b.fabric", "rdma": ["fixture-rdma", None]},
                ],
            }
        )
    )
    settings = Settings(training_enabled=True, sharding_jaccl_hostfile=str(path))
    async with factory.begin() as session:
        await bind_collective_prepares(session, ATTEMPT, settings)
    async with factory.begin() as session:
        commands = list(
            await session.scalars(
                select(TrainingCommandRow).where(
                    TrainingCommandRow.operation == "node.training.prepare"
                )
            )
        )
        prepared = [TrainingPrepareRequest.model_validate(c.payload) for c in commands]
        assert (
            prepared[0].collective == prepared[1].collective and prepared[0].collective is not None
        )
        assert all(
            payload_digest(p) == c.request_sha256 for p, c in zip(prepared, commands, strict=True)
        )
        await bind_collective_prepares(session, ATTEMPT, settings)


@pytest.mark.parametrize("mode", ["missing_rank", "revoked_admin"])
async def test_rank_loss_or_revocation_preserves_uncertain_holds(
    rank_db: RuntimeDatabase, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from coire_api.db import UserRow

    factory, prepare = rank_db
    components = [
        rank_component(prepare, rank, uuid.uuid5(uuid.NAMESPACE_URL, ATTEMPT)) for rank in (0, 1)
    ]
    await stage_ranks(rank_db, components[:1] if mode == "missing_rank" else components)
    fabric = AuthenticatedFabric(components)
    client = client_for(fabric, monkeypatch)
    try:
        async with factory.begin() as session:
            if mode == "missing_rank":
                row = await session.scalar(
                    select(TrainingCommandRow).where(TrainingCommandRow.operation == COMPONENT)
                )
                assert row is not None
                row.created_at = datetime.now(UTC) - timedelta(seconds=61)
            else:
                job = await session.get(TrainingJobRow, JOB)
                assert job is not None
                user = await session.get(UserRow, job.owner_user_id)
                assert user is not None
                user.active = False
        await TrainingComponentCoordinator(client._settings, client).run_once()
        assert not fabric.collections and not fabric.imports
        async with factory.begin() as session:
            job = await session.get(TrainingJobRow, JOB)
            assert job is not None and job.state == "recovering"
            stops = list(
                await session.scalars(
                    select(TrainingCommandRow).where(
                        TrainingCommandRow.operation == "node.training.stop"
                    )
                )
            )
            assert {s.subject_id for s in stops} == {"coire-edge-a", "coire-edge-b"}
            holds = list(await session.scalars(select(MemoryReservationRow)))
            assert all(h.state.value in {"pending", "held", "releasing"} for h in holds)
    finally:
        await client.aclose()


async def test_bundle_substitution_cannot_replace_previous_checkpoint(
    rank_db: RuntimeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_api.db import TrainingCheckpointRow

    factory, prepare = rank_db
    artifact = uuid.uuid4()
    components = [rank_component(prepare, rank, artifact) for rank in (0, 1)]
    await stage_ranks(rank_db, components)
    manifest = TrainingArtifactManifest(
        artifact_id=artifact,
        kind="checkpoint",
        files=[f for c in components for f in c.files],
        total_bytes=60,
        job_id=JOB,
        attempt_id=ATTEMPT,
        fence=1,
        update=1,
        world_size=2,
        runtime_sha256=DIGEST,
        resolved_spec_sha256=payload_digest(prepare.resolved),
        ranks=[c.state for c in components],
    )
    prior_id = uuid.uuid4()
    prior = manifest.model_copy(
        update={
            "artifact_id": prior_id,
            "update": 0,
            "ranks": [r.model_copy(update={"update": 0}) for r in manifest.ranks],
        }
    )
    async with factory.begin() as session:
        session.add(
            TrainingCheckpointRow(
                id=prior_id,
                job_id=JOB,
                attempt_id=ATTEMPT,
                fence=1,
                completed_update=0,
                manifest_sha256=prior.canonical_sha256(),
                manifest=prior.model_dump(mode="json"),
                total_bytes=60,
                state="committed",
            )
        )
        await session.flush()
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None
        job.latest_checkpoint_id = prior_id
        wrong = manifest.model_copy(
            update={
                "files": [
                    manifest.files[0].model_copy(update={"sha256": "b" * 64}),
                    *manifest.files[1:],
                ]
            }
        )
        await ingest_training_event(
            session,
            "coire-edge-a",
            NodeTrainingEvent.model_validate(
                {
                    "sequence": 2,
                    "job_id": JOB,
                    "attempt_id": ATTEMPT,
                    "fence": 1,
                    "update": 1,
                    "recorded_at": datetime.now(UTC),
                    "payload": {"kind": "checkpoint_staged", "manifest": wrong},
                }
            ),
        )
    fabric = AuthenticatedFabric(components)
    client = client_for(fabric, monkeypatch)
    try:
        with pytest.raises(TrainingConflict, match="files"):
            await mirror_checkpoint(artifact, client)
        await TrainingComponentCoordinator(client._settings, client).run_once()
        async with factory.begin() as session:
            job = await session.get(TrainingJobRow, JOB)
            assert (
                job is not None
                and job.latest_checkpoint_id == prior_id
                and job.state == "recovering"
            )
            assert not list(
                await session.scalars(
                    select(TrainingCommandRow).where(
                        TrainingCommandRow.operation == "node.training.checkpoint-commit"
                    )
                )
            )
        assert not fabric.collections
    finally:
        await client.aclose()


async def test_disabled_measurement_recovery_selects_only_existing_dispatches(
    runtime_db: RuntimeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    from training_measurement_fixtures import experiment

    from coire_api.db import TrainingMeasurementRow
    from coire_api.training.runtime import TrainingRuntimeWorker

    factory, _ = runtime_db
    prototype, dispatch = experiment()
    identities = [uuid.uuid4() for _ in range(3)]
    async with factory.begin() as session:
        job = await session.get(TrainingJobRow, JOB)
        assert job is not None
        for index, identity in enumerate(identities):
            session.add(
                TrainingMeasurementRow(
                    id=identity,
                    owner_user_id=job.owner_user_id,
                    request=prototype.request,
                    state="queued" if index == 1 else "running",
                )
            )
            session.add(
                TrainingCommandRow(
                    actor_user_id=job.owner_user_id,
                    idempotency_key=f"measurement-{index}",
                    operation="training.measurement",
                    subject_id=str(identity),
                    request_sha256=DIGEST,
                    payload={"dispatch": dispatch.model_dump(mode="json") if index < 2 else None},
                    state="dispatching",
                )
            )
    worker = TrainingRuntimeWorker(Settings(training_enabled=False))
    calls: list[uuid.UUID] = []

    async def advance(identity: uuid.UUID) -> None:
        calls.append(identity)

    monkeypatch.setattr(worker.measurements, "advance", advance)
    try:
        await worker._recover_measurements_once()
        assert calls == identities[:1]
    finally:
        await worker.stop()
