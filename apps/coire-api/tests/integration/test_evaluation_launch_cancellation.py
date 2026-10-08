"""Cancellation committed during launch cannot be overwritten by a stale instance read."""

import asyncio
import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from evaluation_fixtures import seed_evaluation_resident
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.db import Base, ModelInstanceRow, PlacementDecisionRow
from coire_core.models.instance import InstanceState

pytestmark = pytest.mark.integration


async def test_launch_waits_for_cancel_and_does_not_resurrect_instance(
    training_postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import coire_scheduler.instances as module

    engine = create_async_engine(training_postgres_url)
    launch: asyncio.Task[None] | None = None
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            _, identity = await seed_evaluation_resident(session)
            instance = await session.get(ModelInstanceRow, identity)
            assert instance is not None
            instance.state = InstanceState.REQUESTED
            await session.commit()

        @asynccontextmanager
        async def scope() -> AsyncIterator[AsyncSession]:
            async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
                yield session

        async def no_fallback(*args: object) -> None:
            return None

        monkeypatch.setattr(module, "session_scope", scope)
        monkeypatch.setattr(module, "_wait_for_fallback_teardown", no_fallback)
        async with AsyncSession(engine, expire_on_commit=False) as cancellation:
            instance = await cancellation.get(ModelInstanceRow, identity, with_for_update=True)
            assert instance is not None
            instance.state = InstanceState.FAILED
            await cancellation.flush()
            launch = asyncio.create_task(
                inspect.unwrap(module.execute_instance_launch)(str(identity))
            )
            # Wait for the real competing database transaction, not a scheduling delay.
            async with engine.connect() as observer:
                async with asyncio.timeout(5):
                    for _ in range(500):
                        if await observer.scalar(
                            text(
                                "SELECT count(*) FROM pg_stat_activity WHERE "
                                "datname=current_database() AND wait_event_type='Lock'"
                            )
                        ):
                            break
                        await asyncio.sleep(0.01)
                    else:
                        pytest.fail(
                            "Launch did not wait for the competing cancellation transaction"
                        )
            await cancellation.commit()
        await asyncio.wait_for(launch, timeout=3)
        async with AsyncSession(engine) as session:
            instance = await session.get(ModelInstanceRow, identity)
            assert instance is not None and instance.state is InstanceState.FAILED
            assert await session.scalar(select(func.count()).select_from(PlacementDecisionRow)) == 0
    finally:
        if launch is not None and not launch.done():
            launch.cancel()
            await asyncio.gather(launch, return_exceptions=True)
        await engine.dispose()
