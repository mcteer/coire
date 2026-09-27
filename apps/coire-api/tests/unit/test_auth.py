"""The admin gate (T015, ADR-0004)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from coire_api.auth import (
    ADMIN,
    ANONYMOUS,
    CurrentAdmin,
    PrincipalKind,
    authenticate_request,
    require_admin,
    require_authenticated,
    require_ops_scope,
    require_principal,
)
from coire_core.settings import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():  # type: ignore[no-untyped-def]
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _configure(monkeypatch, token: str) -> None:  # type: ignore[no-untyped-def]
    from pydantic import SecretStr

    import coire_core.settings as cs

    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    settings.admin_token = SecretStr(token)
    settings.identity_legacy_admin_enabled = True
    # `require_principal` imports get_settings inside the call, so patching the module
    # attribute is what takes effect.
    monkeypatch.setattr(cs, "get_settings", lambda: settings)


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


class TestPrincipalResolution:
    async def test_correct_token_is_admin(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        _configure(monkeypatch, "the-token")
        principal = await require_principal(_creds("the-token"))
        assert principal.kind is PrincipalKind.ADMIN
        assert principal.is_admin
        assert principal.subject == "admin-token"

    async def test_anthropic_api_key_header_is_admin(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        _configure(monkeypatch, "the-token")
        principal = await require_principal(None, "the-token")
        assert principal is ADMIN

    async def test_wrong_token_is_anonymous(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        _configure(monkeypatch, "the-token")
        principal = await require_principal(_creds("wrong"))
        assert principal.kind is PrincipalKind.ANONYMOUS
        assert not principal.is_admin

    async def test_missing_header_is_anonymous(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        _configure(monkeypatch, "the-token")
        assert not (await require_principal(None)).is_admin

    async def test_unconfigured_token_makes_nobody_an_admin(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """The failure that would matter most: an unset secret must not open the platform."""
        _configure(monkeypatch, "")
        assert not (await require_principal(_creds(""))).is_admin
        assert not (await require_principal(_creds("anything"))).is_admin
        assert not (await require_principal(None)).is_admin


class TestGuard:
    """`require_admin` refuses with 403 and never leaks whether a token was close."""

    def _app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/api/v1/admin/thing")
        async def thing(principal: CurrentAdmin) -> dict[str, str]:
            return {"subject": principal.subject or ""}

        return app

    async def _call(self, app: FastAPI, headers: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.get("/api/v1/admin/thing", headers=headers or {})

    async def test_admin_passes(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        _configure(monkeypatch, "tok")
        resp = await self._call(self._app(), {"Authorization": "Bearer tok"})
        assert resp.status_code == 200
        assert resp.json()["subject"] == "admin-token"

    @pytest.mark.parametrize(
        "headers",
        [None, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic x"}, {}],
        ids=["no-header", "wrong-token", "wrong-scheme", "empty"],
    )
    async def test_non_admin_is_refused(self, monkeypatch, headers) -> None:  # type: ignore[no-untyped-def]
        _configure(monkeypatch, "tok")

        # The audit path needs a database; the refusal itself must not depend on one.
        @asynccontextmanager
        async def _no_audit(*a, **k):  # type: ignore[no-untyped-def]
            yield None

        monkeypatch.setattr("coire_api.db.session_scope", _no_audit)
        resp = await self._call(self._app(), headers)
        assert resp.status_code == 403

    async def test_refusal_survives_an_audit_failure(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """A broken audit path must never turn a refusal into a success."""
        _configure(monkeypatch, "tok")

        @asynccontextmanager
        async def _boom(*a, **k):  # type: ignore[no-untyped-def]
            yield None

        monkeypatch.setattr("coire_api.db.session_scope", _boom)
        assert (await self._call(self._app(), {"Authorization": "Bearer no"})).status_code == 403


class TestDirectGuardCall:
    async def test_authenticated_guard(self) -> None:
        assert await require_authenticated(ADMIN) is ADMIN
        with pytest.raises(HTTPException) as exc:
            await require_authenticated(ANONYMOUS)
        assert exc.value.status_code == 401
        assert exc.value.headers == {"WWW-Authenticate": "Bearer"}

    async def test_admin_principal_passes_through(self) -> None:
        from coire_api.auth import ADMIN, require_admin

        class Req:
            method = "GET"
            url = type("U", (), {"path": "/api/v1/admin/x"})()
            client = None

        assert await require_admin(Req(), ADMIN) is ADMIN  # type: ignore[arg-type]

    async def test_anonymous_raises_403(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from coire_api.auth import ANONYMOUS, require_admin

        @asynccontextmanager
        async def _boom(*a, **k):  # type: ignore[no-untyped-def]
            yield None

        monkeypatch.setattr("coire_api.db.session_scope", _boom)

        class Req:
            method = "POST"
            url = type("U", (), {"path": "/api/v1/admin/models"})()
            client = None

        with pytest.raises(HTTPException) as exc:
            await require_admin(Req(), ANONYMOUS)  # type: ignore[arg-type]
        assert exc.value.status_code == 403


class TestOpsServicePrincipal:
    async def test_exact_configured_secret_gets_only_fixed_ops_scopes(self) -> None:
        settings = Settings(
            _secrets_dir="/nonexistent",  # type: ignore[call-arg]
            ops_service_token=SecretStr("coire_ops_a-secret-value"),
        )
        request = SimpleNamespace(
            headers={"authorization": "Bearer coire_ops_a-secret-value"},
            app=SimpleNamespace(state=SimpleNamespace(settings=settings)),
        )
        principal = await authenticate_request(request)  # type: ignore[arg-type]
        assert principal.kind is PrincipalKind.OPS_SERVICE
        assert principal.scopes == frozenset(
            {"ops:read", "ops:propose", "ops:session", "ops:infer"}
        )
        assert not principal.is_admin
        assert await require_ops_scope("ops:propose")(principal) is principal

    async def test_wrong_or_unconfigured_ops_secret_fails_closed(self) -> None:
        for expected in ("", "coire_ops_right"):
            settings = Settings(
                _secrets_dir="/nonexistent",  # type: ignore[call-arg]
                ops_service_token=SecretStr(expected),
            )
            request = SimpleNamespace(
                headers={"authorization": "Bearer coire_ops_wrong"},
                app=SimpleNamespace(state=SimpleNamespace(settings=settings)),
            )
            principal = await authenticate_request(request)  # type: ignore[arg-type]
            assert principal is ANONYMOUS

    async def test_ops_service_cannot_cross_human_admin_gate(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        @asynccontextmanager
        async def _no_audit(*a, **k):  # type: ignore[no-untyped-def]
            yield None

        monkeypatch.setattr("coire_api.db.session_scope", _no_audit)
        principal = type(ADMIN)(
            kind=PrincipalKind.OPS_SERVICE,
            subject="coire-ops",
            scopes=frozenset({"ops:propose"}),
        )

        class Req:
            method = "POST"
            url = type("U", (), {"path": "/api/v1/admin/ops/proposals/x/confirm"})()
            client = None

        with pytest.raises(HTTPException) as exc:
            await require_admin(Req(), principal)  # type: ignore[arg-type]
        assert exc.value.status_code == 403
