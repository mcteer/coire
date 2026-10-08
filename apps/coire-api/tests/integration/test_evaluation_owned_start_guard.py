"""A starting owned evaluation engine is not an unmeasured chat resident."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from evaluation_fixtures import seed_evaluation_resident
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.db import (
    Base,
    EvaluationAttemptRow,
    InstanceMemberRow,
    MemoryReservationRow,
    ModelInstanceRow,
    NodeMemoryLedgerRow,
)
from coire_core.models.instance import InstanceState
from coire_core.models.node import Reachability
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_scheduler.evaluation_guard import _guard_reason

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "condition", ["owned", "allocating", "borrowed", "foreign", "stale", "swap"]
)
async def test_start_guard_excludes_only_owned_holds(
    training_postgres_url: str, condition: str
) -> None:
    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            node_id, instance_id = await seed_evaluation_resident(session)
            instance = await session.get(ModelInstanceRow, instance_id)
            assert instance is not None
            instance.state = InstanceState.REQUESTED
            if condition == "allocating":
                member = await session.scalar(
                    select(InstanceMemberRow).where(InstanceMemberRow.instance_id == instance_id)
                )
                assert member is not None
                await session.delete(member)
            session.add(
                NodeMemoryLedgerRow(
                    node_id=node_id,
                    budget_bytes=128 * 1024**3,
                    measured_resident_bytes=0,
                    swap_used_bytes=1 if condition == "swap" else 0,
                    thermal_state="nominal",
                    health=Reachability.HEALTHY,
                    health_sampled_at=datetime.now(UTC)
                    - timedelta(seconds=120 if condition == "stale" else 0),
                )
            )
            if condition == "foreign":
                session.add(
                    MemoryReservationRow(
                        id=uuid.uuid4(),
                        node_id=node_id,
                        holder_type=ReservationHolder.MODEL,
                        holder_id="unknown",
                        bytes=1024,
                        pinned=True,
                        state=MemoryReservationState.HELD,
                    )
                )
            await session.commit()
            attempt = EvaluationAttemptRow(
                node_id=node_id,
                instance_id=instance_id,
                owns_instance=condition != "borrowed",
            )
            expected = (
                None
                if condition in {"owned", "allocating"}
                else ("memory_breach" if condition == "swap" else "telemetry_stale")
            )
            assert await _guard_reason(session, attempt) == expected
    finally:
        await engine.dispose()
