"""Concurrent privileged mutations produce one immutable operation receipt."""

import asyncio
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.db import Base, EvaluationMutationRow, UserRow
from coire_api.evaluation.idempotency import record, replay
from coire_core.errors import EvaluationConflict
from coire_core.models.auth import UserRole
from coire_core.models.evaluation import EvaluationControl

pytestmark = pytest.mark.integration


async def test_concurrent_receipts_replay_and_changed_payload_refuses(
    training_postgres_url: str,
) -> None:
    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        owner = uuid.uuid4()
        async with AsyncSession(engine) as session:
            session.add(
                UserRow(
                    id=owner,
                    email=f"{owner}@idempotency.test",
                    display_name="Admin",
                    role=UserRole.ADMIN,
                    active=True,
                )
            )
            await session.commit()
        body = EvaluationControl(expected_version=1)

        async def submit() -> dict[str, object]:
            async with AsyncSession(engine) as session:
                existing = await replay(
                    session, owner, "evaluation.cancel:fixture", "same-key", body
                )
                if existing is None:
                    await record(
                        session, owner, "evaluation.cancel:fixture", "same-key", body, body
                    )
                    response = body.model_dump(mode="json")
                else:
                    response = existing.response
                await session.commit()
                return response

        responses = await asyncio.gather(submit(), submit())
        assert all(response == body.model_dump() for response in responses)
        async with AsyncSession(engine) as session:
            assert (
                await session.scalar(select(func.count()).select_from(EvaluationMutationRow)) == 1
            )
            with pytest.raises(EvaluationConflict):
                await replay(
                    session,
                    owner,
                    "evaluation.cancel:fixture",
                    "same-key",
                    EvaluationControl(expected_version=2),
                )
            await session.rollback()
            assert (
                await replay(session, owner, "evaluation.rerun:fixture", "same-key", body) is None
            )
    finally:
        await engine.dispose()
