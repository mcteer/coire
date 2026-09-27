from __future__ import annotations

import base64
import time
from collections.abc import Callable

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from coire_api.identity.access import AccessAssertionError, AccessVerifier
from coire_core.settings import Settings


def _b64(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _fixture() -> tuple[Settings, rsa.RSAPrivateKey, dict[str, object]]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_numbers()
    jwk: dict[str, object] = {
        "kty": "RSA",
        "kid": "test-key",
        "use": "sig",
        "alg": "RS256",
        "n": _b64(public.n),
        "e": _b64(public.e),
    }
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    settings.cloudflare_access_issuer = "https://team.cloudflareaccess.com"
    settings.cloudflare_access_audience = "audience"
    settings.bootstrap_admin_email = SecretStr("admin@example.test")
    return settings, key, jwk


def _token(key: rsa.RSAPrivateKey, **overrides: object) -> str:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": "https://team.cloudflareaccess.com",
        "aud": "audience",
        "sub": "identity",
        "email": " Admin@Example.TEST ",
        "iat": now,
        "nbf": now - 1,
        "exp": now + 60,
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})


def _factory(jwk: dict[str, object], calls: list[str]) -> Callable[[], httpx.AsyncClient]:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"keys": [jwk]})

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_valid_assertion_is_verified_and_jwks_is_cached() -> None:
    settings, key, jwk = _fixture()
    calls: list[str] = []
    verifier = AccessVerifier(settings, client_factory=_factory(jwk, calls))
    first = await verifier.verify(_token(key))
    second = await verifier.verify(_token(key))
    assert first["email"] == second["email"] == "admin@example.test"
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "value"),
    [("iss", "https://evil.test"), ("aud", "wrong"), ("exp", 1), ("nbf", 4_000_000_000)],
)
async def test_wrong_claims_are_refused(override: str, value: object) -> None:
    settings, key, jwk = _fixture()
    verifier = AccessVerifier(settings, client_factory=_factory(jwk, []))
    with pytest.raises(AccessAssertionError):
        await verifier.verify(_token(key, **{override: value}))


@pytest.mark.asyncio
async def test_unknown_key_refreshes_once_then_refuses() -> None:
    settings, key, jwk = _fixture()
    jwk["kid"] = "different"
    calls: list[str] = []
    verifier = AccessVerifier(settings, client_factory=_factory(jwk, calls))
    with pytest.raises(AccessAssertionError, match="unknown"):
        await verifier.verify(_token(key))
    assert len(calls) == 2
