"""Every admin route refuses a non-admin (T041, spec SC-002, ADR-0004).

The point of enumerating from the generated OpenAPI document rather than a hand-written list
is that a route added later cannot quietly omit the guard: it appears in the document, so it
appears here.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from coire_api.app import create_app
from coire_core.settings import Settings

ADMIN_TOKEN = "admin-token-for-tests"
PLACEHOLDERS = {"model_id": str(uuid.uuid4()), "engine_id": str(uuid.uuid4())}


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    from pydantic import SecretStr

    import coire_core.settings as cs

    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    settings.admin_token = SecretStr(ADMIN_TOKEN)
    monkeypatch.setattr(cs, "get_settings", lambda: settings)
    monkeypatch.setattr("coire_api.deps.get_settings", lambda: settings)

    # No database and no reconciler: this test is about the guard, which must refuse before
    # touching either. A route that needed a session to say 403 would be the bug.
    async def _no_session():  # type: ignore[no-untyped-def]
        raise AssertionError("a refused request must not open a database session")

    async def _no_audit(*a: Any, **k: Any):  # type: ignore[no-untyped-def]
        raise RuntimeError("no database in this test")

    monkeypatch.setattr("coire_api.db.session_scope", _no_audit)
    application = create_app(settings)
    application.dependency_overrides[
        __import__("coire_api.db", fromlist=["get_session"]).get_session
    ] = _no_session
    return application


def admin_operations(app: Any) -> list[tuple[str, str]]:
    """Every (method, path) under /api/v1/admin from the generated document."""
    spec = app.openapi()
    out: list[tuple[str, str]] = []
    for path, methods in spec["paths"].items():
        if not path.startswith("/api/v1/admin"):
            continue
        for method in methods:
            if method.lower() in ("get", "post", "patch", "delete", "put"):
                out.append((method.upper(), path))
    return sorted(out)


def concrete(path: str) -> str:
    for name, value in PLACEHOLDERS.items():
        path = path.replace(f"{{{name}}}", value)
    return path


class TestEveryAdminRouteIsGuarded:
    def test_there_are_admin_routes_to_guard(self, app: Any) -> None:
        """Guard against the test silently passing because it found nothing."""
        assert len(admin_operations(app)) >= 10

    @pytest.mark.parametrize(
        "headers",
        [None, {"Authorization": "Bearer wrong-token"}, {"Authorization": "Basic abc"}],
        ids=["no-credential", "wrong-token", "wrong-scheme"],
    )
    async def test_non_admins_are_refused_everywhere(
        self, app: Any, headers: dict[str, str] | None
    ) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            for method, path in admin_operations(app):
                resp = await client.request(method, concrete(path), headers=headers or {}, json={})
                assert resp.status_code == 403, f"{method} {path} returned {resp.status_code}"

    async def test_the_user_listing_is_not_admin_gated(self, app: Any) -> None:
        """`/api/v1/models` is the one route in this feature anyone may call."""
        assert "/api/v1/models" in app.openapi()["paths"]
        assert ("GET", "/api/v1/models") not in admin_operations(app)


class TestRefusalLeaksNothing:
    async def test_a_refusal_body_does_not_disclose_the_expected_token(self, app: Any) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.get(
                "/api/v1/admin/models", headers={"Authorization": "Bearer nope"}
            )
        assert resp.status_code == 403
        assert ADMIN_TOKEN not in resp.text

    async def test_a_wrong_token_is_indistinguishable_from_no_token(self, app: Any) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            absent = await client.get("/api/v1/admin/models")
            wrong = await client.get(
                "/api/v1/admin/models", headers={"Authorization": "Bearer nope"}
            )
        assert absent.status_code == wrong.status_code == 403
        assert absent.json() == wrong.json()
