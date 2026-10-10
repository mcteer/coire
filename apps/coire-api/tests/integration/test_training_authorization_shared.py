"""Concurrent readonly authority barriers still block privilege mutation."""

import asyncio
import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_training_measurement_transactions import measurement_db as measurement_db

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import ApiKeyRow, TrainingMeasurementRow, UserRow
from coire_api.training.authorization import authorize_live_training_action
from coire_core.errors import TrainingForbidden
from coire_core.models.auth import UserRole

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("COIRE_INTEGRATION") != "1", reason="disposable Postgres"),
]


@pytest.mark.parametrize("mutation", ["deactivate", "revoke", "rotate", "remove_admin"])
async def test_shared_readers_block_permission_mutation(
    measurement_db: async_sessionmaker[AsyncSession], mutation: str
) -> None:
    factory = measurement_db
    key_id = uuid.uuid4()
    async with factory.begin() as session:
        row = await session.scalar(select(TrainingMeasurementRow))
        assert row is not None
        owner = row.owner_user_id
        session.add(
            ApiKeyRow(
                id=key_id,
                user_id=owner,
                name="shared authority fixture",
                prefix="abcdefghijkl",
                secret_hash="synthetic-hash",
                credential_version=1,
                scopes=["admin"],
                requests_per_minute=1000,
                monthly_budget_tokens=10000000,
            )
        )
    principal = Principal(
        kind=PrincipalKind.API_KEY,
        user_id=owner,
        role=UserRole.ADMIN,
        api_key_id=key_id,
        credential_version=1,
        scopes=frozenset({"admin"}),
    )
    started = asyncio.Event()

    async def mutate() -> None:
        async with factory.begin() as session:
            started.set()
            if mutation == "deactivate":
                statement = update(UserRow).where(UserRow.id == owner).values(active=False)
            else:
                values: dict[str, object] = (
                    {"revoked_at": datetime.now(UTC)}
                    if mutation == "revoke"
                    else {"credential_version": 2}
                    if mutation == "rotate"
                    else {"scopes": []}
                )
                statement = update(ApiKeyRow).where(ApiKeyRow.id == key_id).values(**values)
            await session.execute(statement)

    pending: asyncio.Task[None] | None = None
    try:
        async with factory.begin() as first:
            assert await authorize_live_training_action(first, principal, shared=True) == owner
            async with factory.begin() as second:
                assert (
                    await asyncio.wait_for(
                        authorize_live_training_action(second, principal, shared=True), timeout=1
                    )
                    == owner
                )
                pending = asyncio.create_task(mutate())
                await asyncio.wait_for(started.wait(), timeout=1)
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(pending), timeout=0.1)
        assert pending is not None
        await asyncio.wait_for(pending, timeout=2)
        async with factory.begin() as fresh:
            with pytest.raises(TrainingForbidden):
                await authorize_live_training_action(fresh, principal, shared=True)
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
