"""Live child admission binds the frozen backend without allowing registry drift."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from evaluation_fixtures import FIXTURE, seed_evaluation, seed_evaluation_resident
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.db import (
    Base,
    EvaluationAttemptRow,
    EvaluationRunRow,
    MemoryReservationRow,
    ModelRow,
)
from coire_api.evaluation.execution import create_evaluation_child
from coire_core.errors import EvaluationConflict
from coire_core.models.evaluation import EvaluationSuite, EvaluationWorkload, canonical_digest
from coire_core.models.registry import EngineBackend
from coire_core.settings import Settings

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "frozen,live,allowed",
    [
        (EngineBackend.MLX_LM, EngineBackend.MLX_LM, True),
        (EngineBackend.MLX_VLM, EngineBackend.MLX_VLM, True),
        (EngineBackend.MLX_LM, EngineBackend.MLX_VLM, False),
        (EngineBackend.MLX_VLM, EngineBackend.MLX_LM, False),
    ],
)
async def test_live_child_requires_exact_frozen_backend(
    training_postgres_url: str, frozen: EngineBackend, live: EngineBackend, allowed: bool
) -> None:
    database = create_async_engine(training_postgres_url)
    try:
        async with database.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(database, expire_on_commit=False) as session:
            identity = await seed_evaluation(session)
            node, instance = await seed_evaluation_resident(session)
            parent = await session.get(EvaluationRunRow, identity)
            assert parent is not None
            work = EvaluationWorkload.model_validate_json(FIXTURE.read_bytes())
            target = work.target.model_copy(update={"engine_backend": frozen})
            model = await session.get(ModelRow, target.target.model_id)
            assert model is not None
            model.backend = live.value
            parent.subjects = [target.model_dump(mode="json")]
            parent.state = "reserving"
            parent.started_at = datetime.now(UTC)
            parent.started_deadline_at = parent.started_at + timedelta(minutes=5)
            work = work.model_copy(
                update={
                    "evaluation_id": identity,
                    "attempt_id": uuid.uuid4(),
                    "run_id": uuid.uuid4(),
                    "target": target,
                    "suite": EvaluationSuite.model_validate(parent.suite_snapshot),
                    "deadline": parent.started_deadline_at,
                }
            )
            hold = await session.scalar(
                select(MemoryReservationRow).where(MemoryReservationRow.holder_id == str(instance))
            )
            assert hold is not None
            attempt = EvaluationAttemptRow(
                id=work.attempt_id,
                run_id=identity,
                phase=work.phase,
                ordinal=1,
                fence=parent.fence,
                target_sha256=canonical_digest(target),
                workload=work.model_dump(mode="json"),
                deadline_at=work.deadline,
                state="pending",
                node_id=node,
                instance_id=instance,
                sandbox_reservation_id=hold.id,
            )
            session.add(attempt)
            await session.flush()
            if allowed:
                child = await create_evaluation_child(session, parent, attempt, Settings())
                assert child.evaluation_attempt_id == attempt.id
                assert attempt.agent_run_id == child.id
            else:
                with pytest.raises(EvaluationConflict, match="target is unavailable"):
                    await create_evaluation_child(session, parent, attempt, Settings())
    finally:
        await database.dispose()
