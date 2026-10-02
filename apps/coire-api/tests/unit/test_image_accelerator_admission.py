"""Real cross-process admission: chat cannot race an image/eviction transaction."""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    Base,
    ImageCoexistenceProfileRow,
    MemoryReservationRow,
    ModelRow,
    ModelVariantRow,
    NodeMemoryLedgerRow,
    NodeRow,
    RequestLeaseRow,
)
from coire_api.images import coexistence
from coire_api.placement.service import (
    LedgerNotFoundError,
    _active_leases,
    acquire_lease,
    refresh_lease,
)
from coire_core.models.acquisition import VariantState
from coire_core.models.images import (
    ImageCoexistenceBounds,
    ImageCoexistenceProfile,
    ImageCoexistenceReportRequest,
)
from coire_core.models.node import NodeRole, Reachability
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.models.registry import EngineBackend, ModelKind, ModelState
from coire_scheduler.image_admission import (
    chat_mix_allowed,
    coexistence_report_hash,
    image_available_bytes,
    node_hardware_fingerprint,
    node_runtime_fingerprint,
)
from coire_scheduler.image_dispatch import ImageNodeCandidate, choose_image_node

# Credentials stay in the child environment, never argv or output. This invokes
# production admission helpers in another interpreter, not an asyncio mock.
_LOCK_PROCESS = """
import asyncio, os, sys, uuid
from datetime import UTC, datetime
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from coire_api.db import MemoryReservationRow
from coire_api.placement.service import lock_nodes_for_admission
from coire_core.models.placement import MemoryReservationState
async def main():
    engine = create_async_engine(os.environ['COIRE_TEST_CHILD_DATABASE_URL'])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session, session.begin():
            await lock_nodes_for_admission(session, [uuid.UUID(sys.argv[1])])
            print('locked', flush=True)
            await asyncio.to_thread(sys.stdin.readline)
            if sys.argv[3] == 'release':
                row = await session.get(MemoryReservationRow, uuid.UUID(sys.argv[2]))
                row.state = MemoryReservationState.RELEASED
                row.released_at = datetime.now(UTC)
            elif sys.argv[3] == 'consume':
                row = await session.get(MemoryReservationRow, uuid.UUID(sys.argv[2]))
                row.bytes = 1024 ** 3
    finally:
        await engine.dispose()
asyncio.run(main())
"""


@pytest.fixture
async def admission_database() -> AsyncIterator[
    tuple[async_sessionmaker[AsyncSession], str, uuid.UUID, uuid.UUID]
]:
    admin_dsn = os.environ.get("COIRE_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("set COIRE_TEST_POSTGRES_DSN for disposable local PostgreSQL")
    parsed = urlparse(admin_dsn)
    if parsed.scheme not in {"postgresql", "postgres"} or parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        pytest.fail("cross-process admission requires an explicit localhost PostgreSQL server")
    database = "coire_image_admission_" + uuid.uuid4().hex[:12]
    connection = await asyncpg.connect(admin_dsn)
    try:
        await connection.execute(f"CREATE DATABASE {database}")
    finally:
        await connection.close()
    dsn = urlunparse(parsed._replace(scheme="postgresql+asyncpg", path="/" + database))
    engine = create_async_engine(dsn)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    node_id, reservation_id = uuid.uuid4(), uuid.uuid4()
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=[
                        Base.metadata.tables["nodes"],
                        Base.metadata.tables["memory_reservations"],
                        Base.metadata.tables["request_leases"],
                    ],
                )
            )
        async with sessions() as session, session.begin():
            session.add(
                NodeRow(
                    id=node_id,
                    name="image-admission-fixture",
                    role=NodeRole.STUDIO,
                    memory_total_bytes=1024**3,
                    disk_total_bytes=1024**3,
                    agent_version="fixture",
                    reachability=Reachability.HEALTHY,
                )
            )
            await session.flush()
            session.add(
                MemoryReservationRow(
                    id=reservation_id,
                    node_id=node_id,
                    holder_type=ReservationHolder.MODEL,
                    holder_id=str(uuid.uuid4()),
                    bytes=1024,
                    state=MemoryReservationState.HELD,
                )
            )
        yield sessions, dsn, node_id, reservation_id
    finally:
        await engine.dispose()
        connection = await asyncpg.connect(admin_dsn)
        try:
            await connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
                database,
            )
            await connection.execute(f"DROP DATABASE IF EXISTS {database}")
        finally:
            await connection.close()


@pytest.mark.integration
@pytest.mark.parametrize("release_reservation", [False, True])
async def test_chat_lease_waits_for_independent_node_admission_and_rechecks_reservation(
    admission_database: tuple[async_sessionmaker[AsyncSession], str, uuid.UUID, uuid.UUID],
    release_reservation: bool,
) -> None:
    sessions, dsn, node_id, reservation_id = admission_database
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _LOCK_PROCESS,
        str(node_id),
        str(reservation_id),
        "release" if release_reservation else "keep",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "COIRE_TEST_CHILD_DATABASE_URL": dsn},
    )
    assert child.stdin is not None and child.stdout is not None
    lease_started = asyncio.Event()
    lease_task: asyncio.Task[uuid.UUID] | None = None

    async def lease() -> uuid.UUID:
        async with sessions() as session, session.begin():
            lease_started.set()
            row = await acquire_lease(session, reservation_id, "cross-process-chat", ttl_seconds=60)
            assert row.expires_at > datetime.now(UTC)
            return row.id

    try:
        assert await asyncio.wait_for(child.stdout.readline(), 10) == b"locked\n"
        lease_task = asyncio.create_task(lease())
        await asyncio.wait_for(lease_started.wait(), 5)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(lease_task), 0.15)
        child.stdin.write(b"release admission\n")
        await child.stdin.drain()
        if release_reservation:
            with pytest.raises(LedgerNotFoundError):
                await asyncio.wait_for(lease_task, 5)
        else:
            assert isinstance(await asyncio.wait_for(lease_task, 5), uuid.UUID)
        assert await asyncio.wait_for(child.wait(), 5) == 0
        async with sessions() as session:
            count = await session.scalar(select(func.count()).select_from(RequestLeaseRow))
            assert count == (0 if release_reservation else 1)
    finally:
        if lease_task is not None:
            lease_task.cancel()
            await asyncio.gather(lease_task, return_exceptions=True)
        if child.returncode is None:
            child.kill()
        await child.wait()


@pytest.mark.integration
async def test_expired_chat_lease_cannot_be_revived_or_counted_for_admission(
    admission_database: tuple[async_sessionmaker[AsyncSession], str, uuid.UUID, uuid.UUID],
) -> None:
    sessions, _, _, reservation_id = admission_database
    async with sessions() as session, session.begin():
        expired = await acquire_lease(session, reservation_id, "expired-chat", ttl_seconds=60)
        active = await acquire_lease(session, reservation_id, "active-chat", ttl_seconds=60)
        expired.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        expired_id, active_id = expired.id, active.id
    async with sessions() as session, session.begin():
        assert await _active_leases(session) == {reservation_id: 1}
        assert not await refresh_lease(session, expired_id, ttl_seconds=60)
        assert not await refresh_lease(session, active_id, ttl_seconds=0)
        assert await refresh_lease(session, active_id, ttl_seconds=60)
        assert await _active_leases(session) == {reservation_id: 1}


@pytest.mark.integration
async def test_profile_invalidation_waits_for_independent_node_admission(
    admission_database: tuple[async_sessionmaker[AsyncSession], str, uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions, dsn, node_id, reservation_id = admission_database
    image_id, chat_model_id, variant_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    engine = create_async_engine(dsn)
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync,
                tables=[
                    Base.metadata.tables["models"],
                    Base.metadata.tables["model_variants"],
                    Base.metadata.tables["image_coexistence_profiles"],
                ],
            )
        )
    await engine.dispose()
    async with sessions() as session, session.begin():
        node = await session.get(NodeRow, node_id)
        assert node is not None
        session.add_all(
            [
                ModelRow(
                    id=image_id,
                    repo_id="test/image",
                    slug="test--image",
                    display_name="Image",
                    precision="4bit",
                    weight_bytes=1,
                    total_bytes=1,
                    file_count=1,
                    memory_estimate_bytes=1,
                    kind=ModelKind.IMAGE_MODEL,
                    backend=EngineBackend.MFLUX.value,
                    state=ModelState.READY,
                    image_capability_profile={},
                ),
                ModelRow(
                    id=chat_model_id,
                    repo_id="test/chat",
                    slug="test--chat",
                    display_name="Chat",
                    precision="4bit",
                    weight_bytes=1,
                    total_bytes=1,
                    file_count=1,
                    memory_estimate_bytes=1,
                    kind=ModelKind.LANGUAGE_MODEL,
                    backend=EngineBackend.MLX_LM.value,
                    state=ModelState.READY,
                ),
            ]
        )
        await session.flush()
        session.add(
            ModelVariantRow(
                id=variant_id,
                model_id=chat_model_id,
                name="chat",
                slug="test--chat-variant",
                source_revision="a" * 40,
                precision="4bit",
                state=VariantState.READY,
                validated=True,
                published=True,
            )
        )
        report = ImageCoexistenceReportRequest(
            node_id=node_id,
            image_model_id=image_id,
            chat_variant_ids=(variant_id,),
            hardware_fingerprint=node_hardware_fingerprint(node),
            runtime_fingerprint=node_runtime_fingerprint(node),
            measured_bounds=ImageCoexistenceBounds(
                max_width=512, max_height=512, max_steps=4, max_outputs=1
            ),
            duration_seconds=900,
            prompt_tokens_max=4096,
            first_token_p95_ms=1200,
            gateway_overhead_p95_ms=15,
            image_completed_count=1,
            image_progress_observed=True,
            swap_observed=False,
            thermal_alarm=False,
            runtime_version="mflux-0.20.0",
            measured_at=now - timedelta(minutes=5),
            valid_until=now + timedelta(hours=1),
        )
        profile_id = uuid.uuid4()
        session.add(
            ImageCoexistenceProfileRow(
                id=profile_id,
                profile_hash=coexistence_report_hash(report),
                node_id=node_id,
                hardware_fingerprint=report.hardware_fingerprint,
                runtime_fingerprint=report.runtime_fingerprint,
                chat_variant_ids=[str(variant_id)],
                image_model_id=image_id,
                image_mode="txt2img",
                measured_bounds=report.measured_bounds.model_dump(mode="json"),
                benchmark_result=report.model_dump(mode="json"),
                first_token_p95_ms=1200,
                status="approved",
                valid_until=report.valid_until,
            )
        )
    async with sessions() as session:
        assert await chat_mix_allowed(session, node_id, image_id, {str(variant_id)}, now)
        assert not await chat_mix_allowed(session, node_id, image_id, {str(uuid.uuid4())}, now)

    async def no_audit(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(coexistence, "write_audit", no_audit)
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _LOCK_PROCESS,
        str(node_id),
        str(reservation_id),
        "keep",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "COIRE_TEST_CHILD_DATABASE_URL": dsn},
    )
    assert child.stdin is not None and child.stdout is not None
    task: asyncio.Task[ImageCoexistenceProfile] | None = None
    try:
        assert await asyncio.wait_for(child.stdout.readline(), 10) == b"locked\n"

        async def revoke() -> ImageCoexistenceProfile:
            async with sessions() as session, session.begin():
                return await coexistence.invalidate_coexistence_profile(
                    session,
                    Principal(kind=PrincipalKind.ADMIN, user_id=uuid.uuid4()),
                    profile_id,
                )

        task = asyncio.create_task(revoke())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 0.15)
        child.stdin.write(b"release admission\n")
        await child.stdin.drain()
        result = await asyncio.wait_for(task, 5)
        assert result.status == "invalidated"
        assert await asyncio.wait_for(child.wait(), 5) == 0
        async with sessions() as session:
            assert not await chat_mix_allowed(session, node_id, image_id, {str(variant_id)}, now)
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if child.returncode is None:
            child.kill()
        await child.wait()


@pytest.mark.integration
async def test_pinned_image_rechecks_capacity_after_independent_chat_hold(
    admission_database: tuple[async_sessionmaker[AsyncSession], str, uuid.UUID, uuid.UUID],
) -> None:
    sessions, dsn, node_id, reservation_id = admission_database
    engine = create_async_engine(dsn)
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync,
                tables=[
                    Base.metadata.tables[name]
                    for name in (
                        "models",
                        "model_variants",
                        "placement_decisions",
                        "node_memory_ledgers",
                        "model_instances",
                        "instance_members",
                    )
                ],
            )
        )
    await engine.dispose()
    image_id = uuid.uuid4()
    async with sessions() as session, session.begin():
        session.add(
            NodeMemoryLedgerRow(
                node_id=node_id,
                budget_bytes=1024**3,
                measured_resident_bytes=1024,
                health=Reachability.HEALTHY,
            )
        )

    async def candidate() -> ImageNodeCandidate:
        async with sessions() as session, session.begin():
            from coire_api.placement.service import lock_nodes_for_admission

            await lock_nodes_for_admission(session, [node_id])
            node = await session.get(NodeRow, node_id)
            assert node is not None
            available = await image_available_bytes(
                session, node, image_id, 40, budget_fraction=1.0
            )
            return ImageNodeCandidate(
                name="coire-edge-b",
                node_id=node_id,
                healthy=True,
                memory_total_bytes=available,
                image_busy=False,
                chat_unmeasured=False,
            )

    initial = await candidate()
    assert choose_image_node("pinned:coire-edge-b", 40, [initial]) == initial
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _LOCK_PROCESS,
        str(node_id),
        str(reservation_id),
        "consume",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "COIRE_TEST_CHILD_DATABASE_URL": dsn},
    )
    assert child.stdin is not None and child.stdout is not None
    task: asyncio.Task[ImageNodeCandidate] | None = None
    try:
        assert await asyncio.wait_for(child.stdout.readline(), 10) == b"locked\n"
        task = asyncio.create_task(candidate())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 0.15)
        child.stdin.write(b"release admission\n")
        await child.stdin.drain()
        after_chat = await asyncio.wait_for(task, 5)
        assert await asyncio.wait_for(child.wait(), 5) == 0
        assert after_chat.memory_total_bytes == 0
        other = ImageNodeCandidate(
            name="coire-edge-a",
            node_id=uuid.uuid4(),
            healthy=True,
            memory_total_bytes=1024**3,
            image_busy=False,
            chat_unmeasured=False,
        )
        assert choose_image_node("pinned:coire-edge-b", 40, [after_chat, other]) is None
        assert choose_image_node("single:auto", 40, [after_chat, other]) == other
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if child.returncode is None:
            child.kill()
        await child.wait()
