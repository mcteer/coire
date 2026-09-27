from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, Mock

import pytest

from coire_api.db import ApiKeyRow, UserRow
from coire_api.identity.keys import (
    KEY_PATTERN,
    InvalidApiKey,
    _material,
    authenticate_key,
    hasher,
    key_is_active,
)
from coire_core.models.auth import ActorType, AuthScope, UserRole


def test_generated_key_has_parseable_nonsecret_prefix_and_43_char_secret() -> None:
    prefix, secret, presented = _material()
    assert len(prefix) == 12
    assert len(secret) == 43
    assert KEY_PATTERN.fullmatch(presented).groups() == (prefix, secret)  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_prefix_collision_verifies_all_hashes_not_prefix_alone() -> None:
    prefix = "abcdefghijkl"
    secret = "s" * 43
    user_id = uuid.uuid4()
    wrong = ApiKeyRow(
        id=uuid.uuid4(),
        user_id=user_id,
        name="wrong",
        prefix=prefix,
        secret_hash=hasher.hash("x" * 43),
        credential_version=1,
        scopes=["admin"],
        requests_per_minute=10,
        monthly_budget_tokens=100,
    )
    right = ApiKeyRow(
        id=uuid.uuid4(),
        user_id=user_id,
        name="right",
        prefix=prefix,
        secret_hash=hasher.hash(secret),
        credential_version=3,
        scopes=["chat"],
        requests_per_minute=10,
        monthly_budget_tokens=100,
    )
    result_rows = Mock()
    result_rows.all.return_value = [wrong, right]
    entitlements = Mock()
    entitlements.all.return_value = ["explicit"]
    session = AsyncMock()
    session.scalars.side_effect = [result_rows, entitlements]
    session.get.return_value = UserRow(
        id=user_id,
        email="user@example.test",
        display_name="User",
        role=UserRole.USER,
        active=True,
    )

    principal = await authenticate_key(session, f"coire_{prefix}_{secret}")

    assert principal.actor_type is ActorType.API_KEY
    assert principal.api_key_id == right.id
    assert principal.scopes == frozenset({AuthScope.CHAT})
    assert principal.entitlements == frozenset({"explicit"})


@pytest.mark.asyncio
async def test_malformed_or_unmatched_key_is_refused() -> None:
    session = AsyncMock()
    with pytest.raises(InvalidApiKey):
        await authenticate_key(session, "not-a-key")
    rows = Mock()
    rows.all.return_value = []
    session.scalars.return_value = rows
    with pytest.raises(InvalidApiKey):
        await authenticate_key(session, "coire_abcdefghijkl_" + "s" * 43)


@pytest.mark.asyncio
async def test_rotation_version_and_user_state_are_rechecked() -> None:
    key_id = uuid.uuid4()
    user_id = uuid.uuid4()
    principal = Mock(api_key_id=key_id, user_id=user_id, credential_version=1)
    key = Mock(revoked_at=None, credential_version=2)
    user = Mock(active=True)
    session = AsyncMock()
    session.get.side_effect = [key, user]
    assert not await key_is_active(session, principal)
