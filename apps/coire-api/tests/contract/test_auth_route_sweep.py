from __future__ import annotations

import re
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from coire_api.app import create_app
from coire_core.settings import Settings

EXEMPT = {"/ready", "/health", "/api/v1/nodes/register"}


def _path(template: str) -> str:
    return re.sub(r"\{[^}]+\}", str(uuid.uuid4()), template)


@pytest.fixture
def app(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr("coire_api.auth.audit_authentication_failure", AsyncMock())
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    return create_app(settings)


@pytest.mark.asyncio
async def test_every_application_route_except_declared_health_or_node_auth_refuses_anonymous(
    app: Any,
) -> None:
    checked: list[str] = []
    operations = app.openapi()["paths"]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        for path, methods in operations.items():
            if path in EXEMPT:
                continue
            for method in methods:
                response = await client.request(method, _path(path), json={})
                assert response.status_code == 401, (
                    f"{method} {path} returned {response.status_code}"
                )
                checked.append(f"{method} {path}")
        for path in ("/api/docs", "/api/openapi.json", "/docs/oauth2-redirect", "/redoc"):
            assert (await client.get(path)).status_code == 401
    assert len(checked) >= 25


def test_anonymous_exceptions_are_exact_and_reviewable(app) -> None:  # type: ignore[no-untyped-def]
    paths = set(app.openapi()["paths"])
    assert paths >= EXEMPT
    assert "/api/v1/me" in paths
