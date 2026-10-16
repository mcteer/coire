"""Local auth uses real Postgres and the same middleware as deployed requests."""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from coire_api.app import create_app
from coire_api.db import (
    Base,
    BrowserSessionRow,
    UserRow,
    dispose_engine,
    init_engine,
    session_scope,
)
from coire_api.identity.local import provision_password
from coire_core.models.auth import UserRole
from coire_core.settings import Settings

PASSWORD = "a private test password only"


@pytest.fixture
async def local_client(training_postgres_url: str):  # type: ignore[no-untyped-def]
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(training_postgres_url)
    async with engine.begin() as connection:
        for name in (
            "users",
            "entitlements",
            "audit_log",
            "local_credentials",
            "browser_sessions",
            "local_login_windows",
        ):
            table = Base.metadata.tables.get(name)
            if table is not None:
                await connection.run_sync(
                    lambda conn, table=table: table.create(conn, checkfirst=True)
                )
    await engine.dispose()
    from urllib.parse import urlsplit

    parsed = urlsplit(training_postgres_url)
    settings = Settings(
        _secrets_dir="/nonexistent",
        postgres_host=parsed.hostname,
        postgres_port=parsed.port,
        postgres_user=parsed.username,
        postgres_password=parsed.password,
        postgres_db=parsed.path[1:],
        local_auth_enabled=True,
        local_auth_origin="https://coire.test",
    )  # type: ignore[call-arg]
    init_engine(settings)
    email = f"{uuid.uuid4().hex}@example.test"
    async with session_scope() as session:
        user = UserRow(email=email, display_name="Local test", role=UserRole.ADMIN)
        session.add(user)
        await session.flush()
        await provision_password(session, email, PASSWORD)
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://coire.test",
        headers={"Origin": "https://coire.test"},
    ) as client:
        yield client, email
    await dispose_engine()


async def login(client: httpx.AsyncClient, email: str) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/local/login", json={"email": email, "password": PASSWORD}
    )


@pytest.mark.asyncio
async def test_login_reload_logout_and_replay(local_client):  # type: ignore[no-untyped-def]
    client, email = local_client
    assert (await client.get("/api/v1/me")).status_code == 401
    response = await login(client, email)
    assert response.status_code == 200, response.text
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert PASSWORD not in response.text
    cookie = client.cookies.get("__Host-coire-session")
    assert (await client.get("/api/v1/me")).json()["email"] == email
    assert (await client.post("/api/v1/auth/local/logout")).status_code == 204
    client.cookies.set("__Host-coire-session", cookie)
    assert (await client.get("/api/v1/me")).status_code == 401


@pytest.mark.asyncio
async def test_origin_and_host_protect_every_cookie_mutation(local_client):  # type: ignore[no-untyped-def]
    client, email = local_client
    for headers in ({"Origin": "https://evil.test"}, {"Origin": ""}, {"Host": "evil.test"}):
        assert (
            await client.post(
                "/api/v1/auth/local/login",
                headers=headers,
                json={"email": email, "password": PASSWORD},
            )
        ).status_code == 403
    assert (await login(client, email)).status_code == 200
    assert (
        await client.post("/api/v1/admin/users", headers={"Origin": "https://evil.test"}, json={})
    ).status_code == 403
    assert (
        await client.post("/api/v1/auth/local/logout", headers={"Origin": ""})
    ).status_code == 403


@pytest.mark.asyncio
async def test_reset_expiry_and_disable_invalidate(local_client):  # type: ignore[no-untyped-def]
    client, email = local_client
    assert (await login(client, email)).status_code == 200
    async with session_scope() as session:
        await provision_password(session, email, PASSWORD + " changed")
    assert (await client.get("/api/v1/me")).status_code == 401
    async with session_scope() as session:
        await provision_password(session, email, PASSWORD)
    assert (await login(client, email)).status_code == 200
    async with session_scope() as session:
        rows = (await session.scalars(select(BrowserSessionRow))).all()
        for row in rows:
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert (await client.get("/api/v1/me")).status_code == 401
    assert (await login(client, email)).status_code == 200
    async with session_scope() as session:
        user = await session.scalar(select(UserRow).where(UserRow.email == email))
        user.active = False
    assert (await client.get("/api/v1/me")).status_code == 401
    assert (await login(client, email)).status_code == 401


@pytest.mark.asyncio
async def test_uniform_refusal_and_durable_throttle(local_client):  # type: ignore[no-untyped-def]
    client, email = local_client
    first = await client.post(
        "/api/v1/auth/local/login", json={"email": email, "password": PASSWORD + " wrong"}
    )
    absent = await login(client, "absent@example.test")
    assert first.status_code == absent.status_code == 401
    assert first.json() == absent.json()
    for _ in range(4):
        assert (
            await client.post(
                "/api/v1/auth/local/login", json={"email": email, "password": PASSWORD + " wrong"}
            )
        ).status_code == 401
    limited = await login(client, email)
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0


@pytest.mark.asyncio
async def test_current_role_and_entitlements_are_rechecked(local_client):  # type: ignore[no-untyped-def]
    from coire_api.db import EntitlementRow

    client, email = local_client
    assert (await login(client, email)).status_code == 200
    async with session_scope() as session:
        user = await session.scalar(select(UserRow).where(UserRow.email == email))
        user.role = UserRole.USER
        session.add(EntitlementRow(user_id=user.id, name="test-entitlement", granted_by=user.id))
    identity = await client.get("/api/v1/me")
    assert identity.json()["role"] == "user"
    assert "test-entitlement" in identity.json()["entitlements"]
    assert (await client.get("/api/v1/admin/users")).status_code == 403
    async with session_scope() as session:
        entitlement = await session.scalar(
            select(EntitlementRow).where(EntitlementRow.user_id == user.id)
        )
        entitlement.revoked_at = datetime.now(UTC)
    assert "test-entitlement" not in (await client.get("/api/v1/me")).json()["entitlements"]


@pytest.mark.asyncio
async def test_malformed_credentials_do_not_echo_secrets(local_client):  # type: ignore[no-untyped-def]
    client, email = local_client
    for password in ("short-secret", {"secret": PASSWORD}, PASSWORD * 50):
        response = await client.post(
            "/api/v1/auth/local/login", json={"email": email, "password": password}
        )
        assert response.status_code == 401
        assert "short-secret" not in response.text
        assert PASSWORD not in response.text
    assert (await login(client, email)).status_code == 200
    client.cookies.set("__Host-coire-session", "invalid")
    assert (await client.get("/api/v1/me")).status_code == 401


@pytest.mark.asyncio
async def test_concurrent_login_cannot_bypass_throttle(local_client):  # type: ignore[no-untyped-def]
    import asyncio

    client, email = local_client
    responses = await asyncio.gather(*(login(client, email) for _ in range(8)))
    assert sorted(response.status_code for response in responses) == [200] * 5 + [429] * 3


def test_plaintext_lan_origin_is_not_permitted() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_secrets_dir="/nonexistent", local_auth_origin="http://coire-core.lab:8180")  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_explicit_invalid_credentials_never_fall_back_to_session(local_client):  # type: ignore[no-untyped-def]
    client, email = local_client
    assert (await login(client, email)).status_code == 200
    for headers in (
        {"Authorization": "Basic invalid"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer invalid"},
        {"X-API-Key": "invalid"},
    ):
        assert (await client.get("/api/v1/me", headers=headers)).status_code == 401
        assert (
            await client.post(
                "/api/v1/admin/users", headers={**headers, "Origin": "https://evil.test"}, json={}
            )
        ).status_code == 401
