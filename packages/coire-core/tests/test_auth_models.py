from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from coire_core.models.auth import (
    ApiKey,
    ApiKeyCreate,
    ApiKeyIssued,
    ApiKeyUpdate,
    AuthScope,
    UserCreate,
    UserRole,
)


def test_user_email_is_normalized_and_role_is_closed() -> None:
    user = UserCreate.model_validate(
        {"email": " Admin@Example.TEST ", "display_name": "Admin", "role": "admin"}
    )
    assert user.email == "admin@example.test"
    assert user.role is UserRole.ADMIN
    with pytest.raises(ValidationError):
        UserCreate.model_validate(
            {"email": "not-an-email", "display_name": "Admin", "role": "owner"}
        )


def test_key_scopes_and_limits_are_strict() -> None:
    request = ApiKeyCreate(
        name="chat",
        scopes=frozenset({AuthScope.CHAT}),
        requests_per_minute=10,
        monthly_budget_tokens=100,
    )
    assert request.scopes == frozenset({AuthScope.CHAT})
    with pytest.raises(ValidationError):
        ApiKeyCreate.model_validate(
            {
                "name": "bad",
                "scopes": ["unknown"],
                "requests_per_minute": 0,
                "monthly_budget_tokens": 0,
            }
        )


def test_secret_exists_only_on_issue_projection() -> None:
    metadata = ApiKey(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="chat",
        prefix="abcdefghijkl",
        scopes=frozenset({AuthScope.CHAT}),
        requests_per_minute=10,
        monthly_budget_tokens=100,
        tokens_consumed=0,
        period_resets_at=datetime.now(UTC),
        active=True,
        created_at=datetime.now(UTC),
    )
    issued = ApiKeyIssued(**metadata.model_dump(), secret="x" * 32)
    assert "secret" not in metadata.model_dump()
    assert issued.model_dump()["secret"] == "x" * 32


def test_key_update_requires_a_bounded_change() -> None:
    assert ApiKeyUpdate(monthly_budget_tokens=200).monthly_budget_tokens == 200
    with pytest.raises(ValidationError):
        ApiKeyUpdate()
    with pytest.raises(ValidationError):
        ApiKeyUpdate(scopes=frozenset())
