from __future__ import annotations

import base64
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient
from starlette.routing import Route

import coire_failover
from coire_core.failover_crypto import sign_ed25519
from coire_core.models.failover import (
    ElectionVoteGrant,
    FailoverAccessVerifier,
    FailoverLease,
    FailoverMember,
    FailoverMembershipConfig,
    FailoverModel,
    FailoverRelayRequest,
    FailoverResidentEngine,
    FailoverSnapshot,
    PromotionProof,
)
from coire_failover.app import FailoverRuntime, create_app


def test_failover_surface_has_no_control_plane_or_documentation_routes() -> None:
    paths = {route.path for route in create_app().routes if isinstance(route, Route)}
    assert paths == {"/", "/ready", "/failover/tier", "/v1/models", "/v1/chat/completions"}


@pytest.mark.asyncio
async def test_unconfigured_snapshot_fails_closed_and_marks_degraded_headers() -> None:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        tier = await client.get("/failover/tier")
        models = await client.get("/v1/models")
        ready = await client.get("/ready")
    assert tier.status_code == 200
    assert tier.json()["tier"] == "minimal"
    assert models.status_code == 503
    assert ready.status_code == 503
    assert models.headers["x-coire-tier"] == "minimal"
    assert models.headers["x-coire-persistence"] == "unavailable"


@pytest.mark.asyncio
async def test_completion_without_snapshot_never_reaches_an_engine() -> None:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "model", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 503
    assert response.headers["x-coire-persistence"] == "unavailable"


def _ed25519() -> tuple[str, str]:
    private = Ed25519PrivateKey.generate()
    private_b64 = base64.b64encode(
        private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    ).decode()
    public_b64 = base64.b64encode(
        private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).decode()
    return private_b64, public_b64


def _b64(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _cluster() -> tuple[dict[str, str], dict[str, str], FailoverMembershipConfig]:
    private = {}
    public = {}
    for name in ("coire-core", "coire-edge-a", "coire-edge-b"):
        private[name], public[name] = _ed25519()
    membership = FailoverMembershipConfig(
        epoch=1,
        signing_key_id="core-1",
        members=[
            FailoverMember(name="coire-core", priority=0, public_key=public["coire-core"]),
            FailoverMember(name="coire-edge-a", priority=1, public_key=public["coire-edge-a"]),
            FailoverMember(name="coire-edge-b", priority=2, public_key=public["coire-edge-b"]),
        ],
    )
    return private, public, membership


def _access_client() -> tuple[httpx.AsyncClient, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = key.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "kid": "test-key",
        "use": "sig",
        "alg": "RS256",
        "n": _b64(numbers.n),
        "e": _b64(numbers.e),
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"keys": [jwk]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), key


def _token(key: rsa.RSAPrivateKey, **overrides: object) -> str:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": "https://team.cloudflareaccess.com",
        "aud": "aud",
        "sub": "user",
        "email": "user@example.test",
        "iat": now,
        "nbf": now - 1,
        "exp": now + 60,
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})


def _snapshot(
    private: dict[str, str],
    membership: FailoverMembershipConfig,
    model: FailoverModel,
    *,
    expired: bool = False,
) -> FailoverSnapshot:
    now = datetime.now(UTC)
    unsigned = FailoverSnapshot(
        snapshot_id=uuid4(),
        issued_at=now - timedelta(seconds=1),
        expires_at=now - timedelta(seconds=1) if expired else now + timedelta(minutes=5),
        membership=membership,
        models=[model],
        access_verifier=FailoverAccessVerifier(
            issuer="https://team.cloudflareaccess.com",
            audience="aud",
            jwks_url="https://team.cloudflareaccess.com/cdn-cgi/access/certs",
        ),
        signature="unsigned",
    )
    return unsigned.model_copy(
        update={"signature": sign_ed25519(unsigned.canonical_bytes(), private["coire-core"])}
    )


def _lease(private: dict[str, str], now: datetime) -> FailoverLease:
    grants = []
    for voter in ("coire-edge-a", "coire-edge-b"):
        unsigned = ElectionVoteGrant(
            epoch=1,
            term=2,
            candidate="coire-edge-a",
            voter=voter,
            expires_at=now + timedelta(minutes=5),
            signature="unsigned",
        )
        grants.append(
            unsigned.model_copy(
                update={"signature": sign_ed25519(unsigned.canonical_bytes(), private[voter])}
            )
        )
    return FailoverLease(
        holder="coire-edge-a",
        proof=PromotionProof(epoch=1, term=2, candidate="coire-edge-a", grants=grants),
    )


def _model() -> FailoverModel:
    return FailoverModel(
        id=uuid4(), slug="tiny-test-model", display_name="Tiny", context_window=2048
    )


@pytest.mark.asyncio
async def test_stale_snapshot_and_stale_access_token_fail_closed() -> None:
    private, _, membership = _cluster()
    model = _model()
    client, key = _access_client()
    runtime = FailoverRuntime(
        load_snapshot=lambda: _snapshot(private, membership, model, expired=True),
        load_lease=lambda: None,
        access_client=client,
    )
    app = create_app(runtime)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        stale_snapshot = await http.get(
            "/v1/models", headers={"Cf-Access-Jwt-Assertion": _token(key)}
        )
    assert stale_snapshot.status_code == 503
    runtime.load_snapshot = lambda: _snapshot(private, membership, model)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        stale_token = await http.get(
            "/v1/models",
            headers={"Cf-Access-Jwt-Assertion": _token(key, exp=1)},
        )
    assert stale_token.status_code == 401
    assert stale_token.json()["detail"] == "identity_invalid"


@pytest.mark.asyncio
async def test_resident_completion_streams_without_a_control_plane_write() -> None:
    private, _, membership = _cluster()
    model = _model()
    client, key = _access_client()
    now = datetime.now(UTC)
    engine = FailoverResidentEngine(engine_id=uuid4(), slug=model.slug)
    seen: list[FailoverRelayRequest] = []

    async def relay(body: FailoverRelayRequest, destination: str) -> StreamingResponse:
        seen.append(body)
        assert destination == "local"

        async def chunks() -> AsyncIterator[bytes]:
            yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'

        return StreamingResponse(chunks(), media_type="text/event-stream")

    runtime = FailoverRuntime(
        load_snapshot=lambda: _snapshot(private, membership, model),
        load_lease=lambda: _lease(private, now),
        local_resident=lambda: _resident(engine),
        relay=relay,
        access_client=client,
    )
    app = create_app(runtime)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        missing = await http.post(
            "/v1/chat/completions",
            headers={"Cf-Access-Jwt-Assertion": _token(key)},
            json={"model": str(uuid4()), "messages": [{"role": "user", "content": "hi"}]},
        )
        streamed = await http.post(
            "/v1/chat/completions",
            headers={"Cf-Access-Jwt-Assertion": _token(key)},
            json={
                "model": str(model.id),
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        listed = await http.get("/v1/models", headers={"Cf-Access-Jwt-Assertion": _token(key)})
        ready = await http.get("/ready")
    assert missing.status_code == 503
    assert missing.json()["reason"] == "model_not_resident"
    assert streamed.status_code == 200
    assert streamed.headers["content-type"].startswith("text/event-stream")
    assert "ok" in streamed.text
    assert streamed.headers["x-coire-persistence"] == "unavailable"
    assert streamed.headers["x-coire-tier"] == "degraded_inference"
    assert listed.status_code == 200
    assert listed.json()["data"][0]["id"] == str(model.id)
    assert ready.status_code == 200
    assert seen[0].model_slug == model.slug


def test_failover_package_has_no_database() -> None:
    package = Path(coire_failover.__file__).resolve().parent
    assert all("sqlalchemy" not in path.read_text() for path in package.glob("*.py"))


async def _resident(engine: FailoverResidentEngine) -> list[FailoverResidentEngine]:
    return [engine]
