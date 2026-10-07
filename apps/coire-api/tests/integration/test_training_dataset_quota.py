"""Real Postgres serializes competing private upload/spool reservations."""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import Base, TrainingStorageReservationRow, UserRow
from coire_api.training.quota import finish_upload_hold, reserve_upload, upload_reservation_bytes
from coire_core.errors import TrainingConflict, TrainingQuotaExceeded
from coire_core.models.auth import UserRole
from coire_core.settings import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="enable disposable-Postgres training quota tests",
    ),
]


@pytest.fixture
async def sessions(training_postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(training_postgres_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def principal(sessions: async_sessionmaker[AsyncSession]) -> Principal:
    owner = uuid.uuid4()
    async with sessions() as session:
        await session.execute(
            insert(UserRow).values(
                id=owner,
                email=f"{owner}@dataset.test",
                display_name="Dataset admin",
                role="admin",
                active=True,
            )
        )
        await session.commit()
    return Principal(kind=PrincipalKind.ADMIN, user_id=owner, role=UserRole.ADMIN)


async def test_competing_uploads_reserve_before_either_can_write(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    actors = [await principal(sessions), await principal(sessions)]
    amount = upload_reservation_bytes(1024, Settings())
    settings = Settings(training_enabled=True, training_dataset_quota_bytes=amount)

    async def admit(actor: Principal) -> bool:
        async with sessions() as session:
            try:
                await reserve_upload(
                    session, actor, settings, declared_bytes=1024, subject_id=uuid.uuid4()
                )
                await session.commit()
                return True
            except TrainingQuotaExceeded:
                await session.rollback()
                return False

    assert sorted(await asyncio.gather(*(admit(actor) for actor in actors))) == [False, True]
    async with sessions() as session:
        rows = (await session.scalars(select(TrainingStorageReservationRow))).all()
        assert len(rows) == 1 and rows[0].bytes == amount


async def test_failed_request_keeps_uncertain_bytes_counted_until_cleanup_proof(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    actor = await principal(sessions)
    amount = upload_reservation_bytes(1024, Settings())
    settings = Settings(training_enabled=True, training_dataset_quota_bytes=amount)
    async with sessions() as session:
        hold = await reserve_upload(
            session, actor, settings, declared_bytes=1024, subject_id=uuid.uuid4()
        )
        identity = hold.id
        await session.commit()

    async with sessions() as session:
        await finish_upload_hold(
            session, identity, retained_bytes=0, private_staging_cleanup_proven=False
        )
        await session.commit()
    async with sessions() as session:
        with pytest.raises(TrainingQuotaExceeded):
            await reserve_upload(
                session, actor, settings, declared_bytes=1024, subject_id=uuid.uuid4()
            )
        await session.rollback()
    async with sessions() as session:
        await finish_upload_hold(
            session, identity, retained_bytes=0, private_staging_cleanup_proven=True
        )
        await session.commit()
    async with sessions() as session:
        await reserve_upload(session, actor, settings, declared_bytes=1024, subject_id=uuid.uuid4())
        await session.commit()


async def test_spool_cleanup_cannot_release_an_already_retained_source(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    actor = await principal(sessions)
    settings = Settings(training_enabled=True)
    async with sessions() as session:
        hold = await reserve_upload(
            session, actor, settings, declared_bytes=1024, subject_id=uuid.uuid4()
        )
        identity = hold.id
        await finish_upload_hold(
            session, identity, retained_bytes=128, private_staging_cleanup_proven=True
        )
        await session.commit()
    async with sessions() as session:
        with pytest.raises(TrainingConflict, match="separate verified deletion"):
            await finish_upload_hold(
                session, identity, retained_bytes=0, private_staging_cleanup_proven=True
            )
        await session.rollback()
    async with sessions() as session:
        row = await session.get(TrainingStorageReservationRow, identity)
        assert row is not None and row.state == "retained" and row.bytes == 128
