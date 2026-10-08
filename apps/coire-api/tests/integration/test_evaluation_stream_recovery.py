"""Independent run/group durable replay survives pruning and refuses future cursors."""

import pytest
from evaluation_fixtures import seed_evaluation
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.db import Base, EvaluationRunRow
from coire_api.evaluation import events, groups
from coire_core.errors import EvaluationConflict

pytestmark = pytest.mark.integration


async def test_group_replay_reset_is_independent_of_run_replay_window(
    training_postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        monkeypatch.setattr(events, "MAX_EVENTS", 3)
        monkeypatch.setattr(groups, "MAX_EVENTS", 2)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            identity = await seed_evaluation(session)
            row = await session.get(EvaluationRunRow, identity)
            assert row is not None
            for _ in range(4):
                row.version += 1
                await events.append(session, row)
            await session.commit()
        async with AsyncSession(engine, expire_on_commit=False) as session:
            page = await groups.replay(session, identity, after=0)
            assert page.cursor == 4 and page.reset is not None and not page.events
            assert page.reset.runs[0].version == 5
            replay = await groups.replay(session, identity, after=2)
            assert replay.reset is None and [event.sequence for event in replay.events] == [3, 4]
            run_events, reset = await events.replay(session, identity, after=1)
            assert not reset and [event.sequence for event in run_events] == [2, 3, 4]
            with pytest.raises(EvaluationConflict):
                await groups.replay(session, identity, after=5)
            with pytest.raises(EvaluationConflict):
                await events.replay(session, identity, after=5)
    finally:
        await engine.dispose()
