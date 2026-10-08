"""Restarted admission reports confirmed durable evictions, not a fresh plan."""

import uuid

import pytest
from evaluation_fixtures import seed_evaluation_resident
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.db import (
    Base,
    EvictionEventRow,
    InstanceMemberRow,
    ModelInstanceRow,
    PlacementDecisionRow,
)
from coire_core.models.placement import PlacementState
from coire_scheduler.placement import confirmed_evictions

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("confirmed", [True, False])
async def test_recovery_preserves_confirmed_history_without_a_new_eviction_plan(
    training_postgres_url: str,
    confirmed: bool,
) -> None:
    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            node, instance_id = await seed_evaluation_resident(session)
            instance = await session.get(ModelInstanceRow, instance_id)
            member = await session.scalar(
                select(InstanceMemberRow).where(InstanceMemberRow.instance_id == instance_id)
            )
            assert instance is not None and member is not None and member.reservation_id is not None
            decision = PlacementDecisionRow(
                id=uuid.uuid4(),
                model_id=instance.model_id,
                variant_id=instance.variant_id,
                policy="single:coire-edge-a",
                state=PlacementState.LOADING,
                required_bytes=1,
            )
            session.add(decision)
            await session.flush()
            outcomes = ["requested", "confirmed", "confirmed"] if confirmed else ["requested"]
            for rank, outcome in enumerate(outcomes, 1):
                session.add(
                    EvictionEventRow(
                        decision_id=decision.id,
                        node_id=node,
                        reservation_id=member.reservation_id,
                        lru_rank=rank,
                        skipped=[],
                        outcome=outcome,
                    )
                )
            await session.commit()
            assert await confirmed_evictions(session, decision.id) == (
                [str(member.reservation_id)] if confirmed else []
            )
            assert await confirmed_evictions(session, uuid.uuid4()) == []
    finally:
        await engine.dispose()
