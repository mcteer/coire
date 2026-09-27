from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr

from coire_api.identity.bootstrap import ensure_bootstrap_admin
from coire_core.models.auth import UserRole
from coire_core.settings import Settings


@pytest.mark.asyncio
async def test_empty_bootstrap_identity_is_fail_closed() -> None:
    session = AsyncMock()
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    assert await ensure_bootstrap_admin(session, settings) is None
    session.scalar.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_is_idempotent_for_existing_admin() -> None:
    session = AsyncMock()
    existing = Mock(email="admin@example.test", active=True, role=UserRole.ADMIN)
    session.scalar.return_value = existing
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    settings.bootstrap_admin_email = SecretStr(" Admin@Example.TEST ")
    assert await ensure_bootstrap_admin(session, settings) is existing
    session.add.assert_not_called()
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_creates_only_configured_normalized_admin(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    session = AsyncMock()
    session.add = Mock()
    session.scalar.return_value = None
    audit = AsyncMock()
    monkeypatch.setattr("coire_api.identity.bootstrap.write_audit", audit)
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    settings.bootstrap_admin_email = SecretStr(" Admin@Example.TEST ")

    row = await ensure_bootstrap_admin(session, settings)

    assert row is not None
    assert row.email == "admin@example.test"
    assert row.role is UserRole.ADMIN
    session.add.assert_called_once_with(row)
    session.flush.assert_awaited_once()
    audit.assert_awaited_once()
