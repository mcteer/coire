"""Credential mutations committed while Argon2 runs must invalidate authentication."""

import asyncio
import os
import threading
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_training_measurement_transactions import measurement_db as measurement_db

from coire_api.db import ApiKeyRow, UserRow
from coire_api.identity import keys
from coire_core.models.auth import UserRole

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("COIRE_INTEGRATION") != "1", reason="disposable Postgres"),
]


@pytest.mark.parametrize("change", ["revoke", "rotate", "deactivate"])
async def test_committed_mutation_during_verification_refuses_old_authority(
    measurement_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    owner, key_id = uuid.uuid4(), uuid.uuid4()
    async with measurement_db.begin() as session:
        session.add(
            UserRow(
                id=owner,
                email=f"{owner}@verification.test",
                display_name="Verification",
                role=UserRole.ADMIN,
                active=True,
            )
        )
        await session.flush()
        session.add(
            ApiKeyRow(
                id=key_id,
                user_id=owner,
                name="verification race",
                prefix="abcdefghijkl",
                secret_hash="synthetic-hash",
                credential_version=1,
                scopes=["admin"],
                requests_per_minute=10,
                monthly_budget_tokens=100,
            )
        )
    started, release = threading.Event(), threading.Event()

    def verify(*args: object) -> bool:
        started.set()
        assert release.wait(5)
        return True

    monkeypatch.setattr(keys, "hasher", SimpleNamespace(verify=verify))
    async with measurement_db() as session:
        pending = asyncio.create_task(
            keys.authenticate_key(
                session,
                "coire_abcdefghijkl_" + "s" * 43,
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 2)
            async with measurement_db.begin() as mutator:
                if change == "deactivate":
                    await mutator.execute(
                        update(UserRow).where(UserRow.id == owner).values(active=False)
                    )
                else:
                    values = (
                        {"revoked_at": datetime.now(UTC)}
                        if change == "revoke"
                        else {
                            "credential_version": 2,
                            "secret_hash": "new-synthetic-hash",
                        }
                    )
                    await mutator.execute(
                        update(ApiKeyRow).where(ApiKeyRow.id == key_id).values(**values)
                    )
        finally:
            release.set()
        with pytest.raises(keys.InvalidApiKey):
            await pending


@pytest.mark.parametrize("change", ["revoke", "rotate", "deactivate", "reassign"])
async def test_stream_recheck_reads_committed_identity_despite_stale_session(
    measurement_db: async_sessionmaker[AsyncSession], change: str
) -> None:
    from sqlalchemy import event

    owner, other, key_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with measurement_db.begin() as session:
        session.add_all(
            [
                UserRow(
                    id=user,
                    email=f"{user}@stream.test",
                    display_name="Stream",
                    role=UserRole.ADMIN,
                    active=True,
                )
                for user in (owner, other)
            ]
        )
        await session.flush()
        session.add(
            ApiKeyRow(
                id=key_id,
                user_id=owner,
                name="stream recheck",
                prefix="abcdefghijkl",
                secret_hash="synthetic-hash",
                credential_version=1,
                scopes=["admin"],
                requests_per_minute=10,
                monthly_budget_tokens=100,
            )
        )
    principal = SimpleNamespace(api_key_id=key_id, user_id=owner, credential_version=1)
    async with measurement_db() as reader:
        stale_key = await reader.get(ApiKeyRow, key_id)
        stale_user = await reader.get(UserRow, owner)
        assert stale_key is not None and stale_user is not None
        assert await keys.key_is_active(reader, principal)
        async with measurement_db.begin() as mutator:
            if change == "deactivate":
                await mutator.execute(
                    update(UserRow).where(UserRow.id == owner).values(active=False)
                )
            else:
                mutations: dict[str, dict[str, object]] = {
                    "revoke": {"revoked_at": datetime.now(UTC)},
                    "rotate": {"credential_version": 2},
                    "reassign": {"user_id": other},
                }
                values = mutations[change]
                await mutator.execute(
                    update(ApiKeyRow).where(ApiKeyRow.id == key_id).values(**values)
                )
        statements: list[str] = []
        assert reader.bind is not None

        def counted(conn, cursor, statement, *args):  # type: ignore[no-untyped-def]
            statements.append(statement)

        event.listen(reader.bind.sync_engine, "before_cursor_execute", counted)
        try:
            assert not await keys.key_is_active(reader, principal)
        finally:
            event.remove(reader.bind.sync_engine, "before_cursor_execute", counted)
        assert len(statements) == 1
        assert stale_key.credential_version == 1 and stale_user.active
