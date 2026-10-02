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

from coire_api.db import Base, MemoryReservationRow, NodeRow, RequestLeaseRow
from coire_api.placement.service import (
    LedgerNotFoundError,
    _active_leases,
    acquire_lease,
    refresh_lease,
)
from coire_core.models.node import NodeRole, Reachability
from coire_core.models.placement import MemoryReservationState, ReservationHolder

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
