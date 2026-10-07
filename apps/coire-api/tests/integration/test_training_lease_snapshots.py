"""Real Postgres node-scoped snapshots, conservative ownership and admission races."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_training_measurement_transactions import measurement_db as measurement_db

from coire_api.app import create_app
from coire_api.db import (
    EngineProcessRow,
    InstanceMemberRow,
    MemoryReservationRow,
    ModelInstanceRow,
    ModelVariantRow,
    NodeRow,
    RequestLeaseRow,
)
from coire_api.placement.service import acquire_lease, lock_nodes_for_admission
from coire_api.training.lease_snapshots import node_lease_snapshot
from coire_core.errors import TrainingConflict
from coire_core.models.instance import InstanceState
from coire_core.models.training_node import NodeTrainingLeaseSnapshot
from coire_core.settings import Settings
from coire_node.training.lease_snapshot import TrainingLeaseSnapshotReader

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="requires disposable Postgres 17",
    ),
]


async def seed(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    async with factory.begin() as session:
        node_a = await session.scalar(select(NodeRow).where(NodeRow.name == "coire-edge-a"))
        node_b = await session.scalar(select(NodeRow).where(NodeRow.name == "coire-edge-b"))
        variant = await session.scalar(select(ModelVariantRow))
        assert node_a is not None and node_b is not None and variant is not None
        instance = ModelInstanceRow(
            id=uuid.uuid4(),
            model_id=variant.model_id,
            variant_id=variant.id,
            policy="single:coire-edge-a",
            state="ready",
        )
        sharded = ModelInstanceRow(
            id=uuid.uuid4(),
            model_id=variant.model_id,
            variant_id=variant.id,
            policy="sharded:tp",
            state="ready",
        )
        engine_only = ModelInstanceRow(
            id=uuid.uuid4(),
            model_id=variant.model_id,
            variant_id=variant.id,
            policy="single:coire-edge-a",
            state="ready",
        )
        session.add_all([instance, sharded, engine_only])
        await session.flush()
        hold = MemoryReservationRow(
            id=uuid.uuid4(),
            node_id=node_a.id,
            holder_type="model",
            holder_id=str(instance.id),
            bytes=100,
            state="held",
        )
        session.add(hold)
        await session.flush()
        session.add_all(
            [
                InstanceMemberRow(
                    instance_id=instance.id,
                    node_id=node_a.id,
                    rank=0,
                    reservation_id=hold.id,
                    host=node_a.name,
                    port=12345,
                ),
                InstanceMemberRow(
                    instance_id=sharded.id, node_id=node_a.id, rank=0, host=node_a.name, port=12346
                ),
                InstanceMemberRow(
                    instance_id=sharded.id, node_id=node_b.id, rank=1, host=node_b.name, port=12346
                ),
                EngineProcessRow(
                    id=uuid.uuid4(),
                    node_id=node_a.id,
                    model_id=variant.model_id,
                    variant_id=variant.id,
                    instance_id=engine_only.id,
                    port=12347,
                    pid=123,
                    process_create_time=1,
                    state="ready",
                    estimate_bytes=100,
                ),
            ]
        )
        return node_a.id, hold.id, instance.id, sharded.id, engine_only.id


def lease(hold_id: uuid.UUID, *, expires: datetime, released: bool = False) -> RequestLeaseRow:
    return RequestLeaseRow(
        id=uuid.uuid4(),
        reservation_id=hold_id,
        request_id=str(uuid.uuid4()),
        expires_at=expires,
        released_at=datetime.now(UTC) if released else None,
    )


async def test_authenticated_route_uses_only_declared_node_bearer_and_node_scope(
    measurement_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, hold_id, instance_id, sharded_id, engine_only_id = await seed(measurement_db)
    async with measurement_db.begin() as session:
        session.add(lease(hold_id, expires=datetime.now(UTC) + timedelta(minutes=1)))

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with measurement_db.begin() as session:
            yield session

    async def forbidden_user_auth(_request: object) -> None:
        raise AssertionError("internal snapshot must not use current-user authentication")

    monkeypatch.setattr("coire_api.routes.internal_training.session_scope", scope)
    monkeypatch.setattr("coire_api.auth.authenticate_request", forbidden_user_auth)
    app = create_app(
        Settings(node_tokens=SecretStr('{"coire-edge-a":"token-a","coire-edge-b":"token-b"}'))
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = "/api/v1/internal/training/nodes/coire-edge-a/leases"
        for headers in (
            {},
            {"X-Coire-Node": "coire-edge-a"},
            {"X-Coire-Node": "coire-edge-a", "Authorization": "Bearer token-b"},
            {"X-Coire-Node": "coire-edge-a", "Authorization": "token-a"},
            {"X-Coire-Node": "coire-edge-b", "Authorization": "Bearer token-b"},
            {"X-Coire-Node": "coire-edge-a", "Authorization": "Bearer coire_human_admin"},
        ):
            assert (await client.get(path, headers=headers)).status_code == 401
        # The snapshot uses Postgres's clock, which can differ from the host
        # clock when the disposable database runs in a virtual machine.
        async with measurement_db.begin() as session:
            before = (await session.execute(select(func.clock_timestamp()))).scalar_one()
        response = await client.get(
            path,
            headers={
                "X-Coire-Node": "coire-edge-a",
                "Authorization": "Bearer token-a",
                "X-Coire-Sampled-At": "2099-01-01T00:00:00Z",
            },
        )
        async with measurement_db.begin() as session:
            after = (await session.execute(select(func.clock_timestamp()))).scalar_one()
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        snapshot = NodeTrainingLeaseSnapshot.model_validate_json(response.content, strict=True)
        assert before <= snapshot.sampled_at <= after
        assert snapshot.expires_at - snapshot.sampled_at == timedelta(seconds=5)
        assert snapshot.active_leases == {instance_id: 1, sharded_id: 0, engine_only_id: 0}
        response_b = await client.get(
            "/api/v1/internal/training/nodes/coire-edge-b/leases",
            headers={
                "X-Coire-Node": "coire-edge-b",
                "Authorization": "Bearer token-b",
            },
        )
        snapshot_b = NodeTrainingLeaseSnapshot.model_validate_json(response_b.content, strict=True)
        assert snapshot_b.active_leases == {sharded_id: 0}
        assert app.openapi()["paths"]["/api/v1/internal/training/nodes/{node}/leases"]["get"][
            "responses"
        ]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
            "/NodeTrainingLeaseSnapshot"
        )
    reader = TrainingLeaseSnapshotReader(
        Settings(
            node_name="coire-edge-a",
            node_token=SecretStr("token-a"),
            core_control_host="coire-core.lab",
            training_input_api_url="http://coire-core.lab:8000",
        ),
        transport=httpx.ASGITransport(app=app),
    )
    try:
        await reader.refresh()
        assert reader({instance_id}) == 1 and reader({engine_only_id}) == 0 and reader(set()) == 1
        assert reader.snapshot is not None
        expiry = reader.snapshot.expires_at
        reader.now = lambda: expiry
        with pytest.raises(TrainingConflict, match="stale"):
            reader({instance_id})
    finally:
        await reader.aclose()


async def test_counts_active_leases_zeroes_and_legacy_or_unowned_holders_conservatively(
    measurement_db: async_sessionmaker[AsyncSession],
) -> None:
    node_id, hold_id, instance_id, sharded_id, engine_only_id = await seed(measurement_db)
    now = datetime.now(UTC)
    async with measurement_db.begin() as session:
        variant = await session.scalar(select(ModelVariantRow))
        assert variant is not None
        legacy = MemoryReservationRow(
            id=uuid.uuid4(),
            node_id=node_id,
            holder_type="model",
            holder_id=str(variant.model_id),
            bytes=100,
            state="held",
        )
        malformed = MemoryReservationRow(
            id=uuid.uuid4(),
            node_id=node_id,
            holder_type="model",
            holder_id="unknown-owner",
            bytes=100,
            state="releasing",
        )
        other = MemoryReservationRow(
            id=uuid.uuid4(),
            node_id=node_id,
            holder_type="sandbox",
            holder_id="opaque-lease",
            bytes=100,
            state="released",
        )
        session.add_all([legacy, malformed, other])
        await session.flush()
        session.add_all(
            [
                lease(hold_id, expires=now + timedelta(minutes=1)),
                lease(hold_id, expires=now + timedelta(minutes=1)),
                lease(hold_id, expires=now - timedelta(seconds=1)),
                lease(hold_id, expires=now + timedelta(minutes=1), released=True),
                lease(legacy.id, expires=now + timedelta(minutes=1)),
                lease(legacy.id, expires=now + timedelta(minutes=1)),
                lease(other.id, expires=now + timedelta(minutes=1)),
                EngineProcessRow(
                    id=uuid.uuid4(),
                    node_id=node_id,
                    model_id=variant.model_id,
                    port=12348,
                    pid=124,
                    process_create_time=1,
                    state="orphan",
                    estimate_bytes=100,
                ),
            ]
        )
        legacy_id, malformed_id, other_id = variant.model_id, malformed.id, other.id
    async with measurement_db.begin() as session:
        snapshot = await node_lease_snapshot(session, "coire-edge-a")
        assert snapshot.active_leases == {
            instance_id: 2,
            sharded_id: 0,
            engine_only_id: 0,
            legacy_id: 2,
            malformed_id: 1,
            other_id: 1,
        }
        # Empty-scope native memory probes sum ALL entries, including unknown ownership.
        assert sum(snapshot.active_leases.values()) == 6
    async with measurement_db.begin() as session:
        instance = await session.get(ModelInstanceRow, instance_id)
        assert instance is not None
        instance.state = InstanceState.STOPPED
        for request_lease in await session.scalars(
            select(RequestLeaseRow).where(
                RequestLeaseRow.reservation_id == hold_id,
            )
        ):
            request_lease.released_at = datetime.now(UTC)
    async with measurement_db.begin() as session:
        protected = await node_lease_snapshot(session, "coire-edge-a")
        # A retained counted hold protects this exact instance even after serving stops.
        assert protected.active_leases[instance_id] == 0


async def test_snapshot_waits_for_new_lease_commit_and_samples_after_admission_lock(
    measurement_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, hold_id, instance_id, _, _ = await seed(measurement_db)
    held, waiting, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def writer() -> datetime:
        async with measurement_db.begin() as session:
            await acquire_lease(session, hold_id, str(uuid.uuid4()), ttl_seconds=30)
            held.set()
            await release.wait()
            observed = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            assert isinstance(observed, datetime)
            return observed

    async def observed_lock(session: AsyncSession, nodes: list[uuid.UUID]) -> None:
        waiting.set()
        await lock_nodes_for_admission(session, nodes)

    monkeypatch.setattr(
        "coire_api.training.lease_snapshots.lock_nodes_for_admission", observed_lock
    )

    async def reader() -> NodeTrainingLeaseSnapshot:
        async with measurement_db.begin() as session:
            return await node_lease_snapshot(session, "coire-edge-a")

    write_task = asyncio.create_task(writer())
    await asyncio.wait_for(held.wait(), 5)
    read_task = asyncio.create_task(reader())
    await asyncio.wait_for(waiting.wait(), 5)
    assert not read_task.done()
    release.set()
    committed_after, snapshot = await asyncio.wait_for(asyncio.gather(write_task, read_task), 5)
    assert snapshot.sampled_at >= committed_after
    assert snapshot.active_leases[instance_id] == 1
    assert snapshot.expires_at - snapshot.sampled_at <= timedelta(seconds=5)


async def test_new_lease_cannot_interleave_inside_snapshot_transaction(
    measurement_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, hold_id, instance_id, _, _ = await seed(measurement_db)
    held, waiting, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def reader() -> NodeTrainingLeaseSnapshot:
        async with measurement_db.begin() as session:
            snapshot = await node_lease_snapshot(session, "coire-edge-a")
            held.set()
            await release.wait()
            return snapshot

    async def observed_lock(session: AsyncSession, nodes: list[uuid.UUID]) -> None:
        waiting.set()
        await lock_nodes_for_admission(session, nodes)

    monkeypatch.setattr("coire_api.placement.service.lock_nodes_for_admission", observed_lock)

    async def writer() -> None:
        async with measurement_db.begin() as session:
            await acquire_lease(session, hold_id, str(uuid.uuid4()), ttl_seconds=30)

    read_task = asyncio.create_task(reader())
    await asyncio.wait_for(held.wait(), 5)
    write_task = asyncio.create_task(writer())
    await asyncio.wait_for(waiting.wait(), 5)
    assert not write_task.done()
    release.set()
    snapshot, _ = await asyncio.wait_for(asyncio.gather(read_task, write_task), 5)
    assert snapshot.active_leases[instance_id] == 0
    async with measurement_db.begin() as session:
        assert (await node_lease_snapshot(session, "coire-edge-a")).active_leases[instance_id] == 1


async def test_snapshot_bound_refuses_truncation_or_ambiguous_ownership(
    measurement_db: async_sessionmaker[AsyncSession],
) -> None:
    node_id, hold_id, _, _, _ = await seed(measurement_db)
    async with measurement_db.begin() as session:
        variant = await session.scalar(select(ModelVariantRow))
        assert variant is not None
        instances = [
            ModelInstanceRow(
                id=uuid.uuid4(),
                model_id=variant.model_id,
                variant_id=variant.id,
                policy="single:coire-edge-a",
                state="ready",
            )
            for _ in range(253)
        ]
        history = [
            ModelInstanceRow(
                id=uuid.uuid4(),
                model_id=variant.model_id,
                variant_id=variant.id,
                policy="single:coire-edge-a",
                state=state,
            )
            for state in ("stopped", "failed")
        ]
        session.add_all(instances + history)
        await session.flush()
        session.add_all(
            [
                InstanceMemberRow(
                    instance_id=i.id,
                    node_id=node_id,
                    rank=0,
                    host="coire-edge-a",
                    port=12349,
                )
                for i in instances + history
            ]
        )
    async with measurement_db.begin() as session:
        assert len((await node_lease_snapshot(session, "coire-edge-a")).active_leases) == 256
        extra = ModelInstanceRow(
            id=uuid.uuid4(),
            model_id=variant.model_id,
            variant_id=variant.id,
            policy="single:coire-edge-a",
            state="ready",
        )
        session.add(extra)
        await session.flush()
        session.add(
            InstanceMemberRow(
                instance_id=extra.id, node_id=node_id, rank=0, host="coire-edge-a", port=12350
            )
        )
    async with measurement_db.begin() as session:
        with pytest.raises(TrainingConflict, match="bound"):
            await node_lease_snapshot(session, "coire-edge-a")
    async with measurement_db.begin() as session:
        member = await session.scalar(
            select(InstanceMemberRow).where(InstanceMemberRow.instance_id == extra.id)
        )
        assert member is not None
        await session.delete(member)
        second = await session.scalar(
            select(InstanceMemberRow)
            .join(ModelInstanceRow, ModelInstanceRow.id == InstanceMemberRow.instance_id)
            .where(
                InstanceMemberRow.reservation_id.is_(None),
                InstanceMemberRow.node_id == node_id,
                ModelInstanceRow.state == "ready",
            )
        )
        assert second is not None
        second.reservation_id = hold_id
    async with measurement_db.begin() as session:
        with pytest.raises(TrainingConflict, match="ambiguous"):
            await node_lease_snapshot(session, "coire-edge-a")
