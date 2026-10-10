"""Memory-hard verification must leave the gateway loop responsive and fail closed."""

import asyncio
import threading
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from coire_api.db import ApiKeyRow, UserRow
from coire_api.identity import keys
from coire_core.models.auth import UserRole


def authority() -> tuple[AsyncMock, ApiKeyRow, UserRow]:
    user = UserRow(id=uuid.uuid4(), role=UserRole.ADMIN, active=True)
    key = ApiKeyRow(
        id=uuid.uuid4(),
        user_id=user.id,
        prefix="abcdefghijkl",
        secret_hash="synthetic-hash",
        credential_version=1,
        scopes=["admin"],
        revoked_at=None,
    )
    candidates, entitlements = Mock(), Mock()
    candidates.all.return_value = [key]
    entitlements.all.return_value = []
    session = AsyncMock()
    session.scalars.side_effect = [candidates, entitlements]
    session.get.side_effect = [key, user]
    return session, key, user


async def authenticate(session: AsyncMock) -> object:
    return await keys.authenticate_key(session, "coire_abcdefghijkl_" + "s" * 43)


async def test_verification_does_not_block_the_gateway_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _, _ = authority()
    loop_thread = threading.get_ident()
    started, release = threading.Event(), threading.Event()

    def verify(*args: object) -> bool:
        assert threading.get_ident() != loop_thread, "Argon2 blocked the gateway event loop"
        started.set()
        assert release.wait(2)
        return True

    monkeypatch.setattr(keys, "hasher", Mock(verify=verify))
    task = asyncio.create_task(authenticate(session))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        # Reaching this line before releasing verification proves loop progress.
        assert not task.done()
    finally:
        release.set()
        await task


@pytest.mark.parametrize("change", ["revoke", "rotate", "hash", "delete", "deactivate"])
async def test_credential_changed_during_verification_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    session, key, user = authority()

    def verify(*args: object) -> bool:
        if change == "revoke":
            key.revoked_at = datetime.now(UTC)
        elif change == "rotate":
            key.credential_version += 1
        elif change == "hash":
            key.secret_hash = "replacement-synthetic-hash"
        elif change == "delete":
            session.get.side_effect = [None]
        else:
            user.active = False
        return True

    monkeypatch.setattr(keys, "hasher", Mock(verify=verify))
    with pytest.raises(keys.InvalidApiKey):
        await authenticate(session)


async def test_verifications_keep_the_existing_single_hash_memory_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_thread = threading.get_ident()
    first_started, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    count = 0
    active = 0
    peak = 0

    def verify(*args: object) -> bool:
        nonlocal count, active, peak
        assert threading.get_ident() != loop_thread
        with lock:
            count += 1
            active += 1
            peak = max(peak, active)
            first_started.set()
        assert release.wait(2)
        with lock:
            active -= 1
        return True

    monkeypatch.setattr(keys, "hasher", Mock(verify=verify))
    tasks = [asyncio.create_task(authenticate(authority()[0])) for _ in range(4)]
    try:
        assert await asyncio.to_thread(first_started.wait, 1)
        await asyncio.sleep(0.02)
        assert count == 1
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert peak == 1 and count == 4
