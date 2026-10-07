"""Real PostgreSQL reduction: immutable updates and all-rank stop proofs."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_training_transactions import ATTEMPT, DIGEST, JOB
from test_training_transactions import database as database

from coire_api.db import (
    MemoryReservationRow,
    NodeRow,
    TrainingAttemptRow,
    TrainingCheckpointRow,
    TrainingEventRow,
    TrainingJobRow,
    TrainingMetricRow,
    TrainingParticipantRow,
)
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_scheduler.training_metrics import load_training_metrics

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="requires disposable Postgres: COIRE_INTEGRATION=1",
    ),
]


async def test_persisted_stalls_checkpoints_and_all_rank_proof(
    database: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with database.begin() as session:
        for rank, node in enumerate(
            (await session.scalars(select(NodeRow).order_by(NodeRow.name))).all()
        ):
            hold = MemoryReservationRow(
                id=uuid.uuid4(),
                node_id=node.id,
                holder_type=ReservationHolder.TRAINING,
                holder_id=ATTEMPT,
                bytes=100,
                pinned=True,
                state=MemoryReservationState.HELD,
            )
            session.add(hold)
            await session.flush()
            session.add(
                TrainingParticipantRow(
                    attempt_id=ATTEMPT,
                    node_id=node.id,
                    rank=rank,
                    reservation_id=hold.id,
                    command_id=uuid.uuid4(),
                    request_sha256=DIGEST,
                    spawn_nonce=uuid.uuid4(),
                )
            )
        await session.execute(
            update(TrainingJobRow)
            .where(TrainingJobRow.id == JOB)
            .values(updated_at=now - timedelta(seconds=450))
        )
        await session.execute(
            update(TrainingAttemptRow)
            .where(TrainingAttemptRow.id == ATTEMPT)
            .values(
                world_size=2,
                created_at=now - timedelta(seconds=500),
                lease_expires_at=now + timedelta(seconds=30),
            )
        )
        session.add(
            TrainingMetricRow(
                job_id=JOB,
                attempt_id=ATTEMPT,
                completed_update=1,
                kind="train",
                loss=1.0,
                metric={},
                recorded_at=now - timedelta(seconds=400),
            )
        )
        session.add(
            TrainingMetricRow(
                job_id=JOB,
                attempt_id=ATTEMPT,
                completed_update=2,
                kind="train",
                loss=1.0,
                metric={},
                rolled_back=True,
                recorded_at=now,
            )
        )
        session.add(
            TrainingMetricRow(
                job_id=JOB,
                attempt_id=ATTEMPT,
                completed_update=2,
                kind="validation",
                loss=1.0,
                metric={},
                recorded_at=now,
            )
        )
        session.add(
            TrainingCheckpointRow(
                id=uuid.uuid4(),
                job_id=JOB,
                attempt_id=ATTEMPT,
                fence=1,
                completed_update=1,
                manifest_sha256=DIGEST,
                manifest={},
                total_bytes=1,
                state="staging",
                created_at=now - timedelta(seconds=70),
            )
        )
    async with database() as session:
        snapshot = await load_training_metrics(session)
    assert snapshot.jobs["running"] == 1
    assert 400 <= snapshot.progress_oldest < 410
    assert 70 <= snapshot.checkpoint_pending_oldest < 80
    async with database.begin() as session:
        await session.execute(
            update(TrainingJobRow)
            .where(TrainingJobRow.id == JOB)
            .values(state="pausing", safe_reason="memory_breach", updated_at=now)
        )
        for sequence, at in [(1, now - timedelta(seconds=65)), (2, now)]:
            session.add(
                TrainingEventRow(
                    job_id=JOB,
                    sequence=sequence,
                    state_version=1,
                    payload={"kind": "state", "state": "pausing"},
                    occurred_at=at,
                )
            )
    async with database() as session:
        overdue = await load_training_metrics(session)
    assert overdue.guard_overdue["memory_breach"] == 1
    async with database.begin() as session:
        await session.execute(
            update(TrainingCheckpointRow).values(state="committed", committed_at=now)
        )
        rank = await session.scalar(
            select(TrainingParticipantRow.id).where(
                TrainingParticipantRow.attempt_id == ATTEMPT, TrainingParticipantRow.rank == 0
            )
        )
        await session.execute(
            update(TrainingParticipantRow)
            .where(TrainingParticipantRow.id == rank)
            .values(stopped_at=now, stop_proof={"stopped": True})
        )
    async with database() as session:
        partial = await load_training_metrics(session)
    assert partial.guard_overdue["memory_breach"] == 1
    assert partial.checkpoint_pending_oldest == 0
    async with database.begin() as session:
        await session.execute(
            update(TrainingParticipantRow)
            .where(TrainingParticipantRow.attempt_id == ATTEMPT)
            .values(stopped_at=now, stop_proof={"stopped": True})
        )
    async with database() as session:
        cleared = await load_training_metrics(session)
    assert all(v == 0 for v in cleared.guard_overdue.values())


async def test_recovery_age_survives_repeat_events_and_restart(
    database: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with database.begin() as session:
        await session.execute(
            update(TrainingJobRow)
            .where(TrainingJobRow.id == JOB)
            .values(state="recovering", updated_at=now)
        )
        for sequence, at in [(1, now - timedelta(seconds=90)), (2, now)]:
            session.add(
                TrainingEventRow(
                    job_id=JOB,
                    sequence=sequence,
                    state_version=1,
                    payload={"kind": "state", "state": "recovering"},
                    occurred_at=at,
                )
            )
    async with database() as session:
        before = await load_training_metrics(session)
    async with database() as session:
        after = await load_training_metrics(session)
    assert before.recovery_oldest >= 90
    assert after.recovery_oldest >= before.recovery_oldest


@pytest.mark.parametrize("disposition", ["cancelled", "fenced", "current"])
async def test_abandoned_checkpoint_alarm_requires_all_rank_death_proof(
    database: async_sessionmaker[AsyncSession], disposition: str
) -> None:
    now = datetime.now(UTC)
    async with database.begin() as session:
        await session.execute(
            update(TrainingJobRow)
            .where(TrainingJobRow.id == JOB)
            .values(
                state="cancelled" if disposition == "cancelled" else "running",
                fence=2 if disposition == "fenced" else 1,
            )
        )
        await session.execute(
            update(TrainingAttemptRow).where(TrainingAttemptRow.id == ATTEMPT).values(world_size=2)
        )
        for rank, node in enumerate(
            (await session.scalars(select(NodeRow).order_by(NodeRow.name))).all()
        ):
            hold = MemoryReservationRow(
                id=uuid.uuid4(),
                node_id=node.id,
                holder_type=ReservationHolder.TRAINING,
                holder_id=ATTEMPT,
                bytes=100,
                pinned=True,
                state=MemoryReservationState.HELD,
            )
            session.add(hold)
            await session.flush()
            session.add(
                TrainingParticipantRow(
                    attempt_id=ATTEMPT,
                    node_id=node.id,
                    rank=rank,
                    reservation_id=hold.id,
                    command_id=uuid.uuid4(),
                    request_sha256=DIGEST,
                    spawn_nonce=uuid.uuid4(),
                )
            )
        session.add(
            TrainingCheckpointRow(
                id=uuid.uuid4(),
                job_id=JOB,
                attempt_id=ATTEMPT,
                fence=1,
                completed_update=1,
                manifest_sha256=DIGEST,
                manifest={},
                total_bytes=1,
                state="replicating",
                created_at=now - timedelta(seconds=70),
            )
        )
    for rank in (0, 1):
        async with database() as session:
            assert (await load_training_metrics(session)).checkpoint_pending_oldest >= 70
        async with database.begin() as session:
            await session.execute(
                update(TrainingParticipantRow)
                .where(
                    TrainingParticipantRow.attempt_id == ATTEMPT,
                    TrainingParticipantRow.rank == rank,
                )
                .values(stopped_at=now, stop_proof={"stopped": True})
            )
    async with database() as session:
        snapshot = await load_training_metrics(session)
    if disposition == "current":
        assert snapshot.checkpoint_pending_oldest >= 70
    else:
        assert snapshot.checkpoint_pending_oldest == 0
