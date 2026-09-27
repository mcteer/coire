"""Strict Cloudflare Access JWT validation against a bounded JWKS cache."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt
from jwt import PyJWK

from coire_core.models.auth import normalize_email
from coire_core.settings import Settings


class AccessAssertionError(ValueError):
    pass


class AccessVerifier:
    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._issuer = settings.cloudflare_access_issuer.rstrip("/")
        self._audience = settings.cloudflare_access_audience
        self._ttl = settings.cloudflare_jwks_ttl_s
        self._leeway = settings.cloudflare_jwt_leeway_s
        self._client_factory = client_factory or httpx.AsyncClient
        self._keys: dict[str, PyJWK] = {}
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._issuer and self._audience)

    async def verify(self, assertion: str) -> dict[str, Any]:
        if not self.configured or not assertion:
            raise AccessAssertionError("Access verification is not configured")
        try:
            kid = str(jwt.get_unverified_header(assertion)["kid"])
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
            raise AccessAssertionError("malformed Access assertion") from exc
        key = await self._key(kid)
        if key is None:
            key = await self._key(kid, force=True)
        if key is None:
            raise AccessAssertionError("Access signing key is unknown")
        try:
            claims = jwt.decode(
                assertion,
                key.key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iss", "aud", "sub", "email"]},
            )
            claims["email"] = normalize_email(str(claims["email"]))
            return claims
        except (jwt.PyJWTError, ValueError) as exc:
            raise AccessAssertionError("Access assertion validation failed") from exc

    async def _key(self, kid: str, *, force: bool = False) -> PyJWK | None:
        if not force and time.monotonic() < self._expires_at:
            return self._keys.get(kid)
        async with self._lock:
            if not force and time.monotonic() < self._expires_at:
                return self._keys.get(kid)
            await self._refresh()
            return self._keys.get(kid)

    async def _refresh(self) -> None:
        try:
            async with self._client_factory() as client:
                response = await client.get(f"{self._issuer}/cdn-cgi/access/certs", timeout=10.0)
                response.raise_for_status()
                payload = response.json()
            keys = payload.get("keys") if isinstance(payload, dict) else None
            if not isinstance(keys, list):
                raise AccessAssertionError("Access JWKS has no keys")
            parsed = {
                str(item["kid"]): PyJWK.from_dict(item, algorithm="RS256")
                for item in keys
                if isinstance(item, dict) and item.get("kid")
            }
            if not parsed:
                raise AccessAssertionError("Access JWKS has no usable keys")
            self._keys = parsed
            self._expires_at = time.monotonic() + self._ttl
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise AccessAssertionError("Access JWKS refresh failed") from exc
