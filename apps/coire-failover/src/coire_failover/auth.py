"""Cloudflare Access validation from the signed snapshot's public verifier settings."""

from __future__ import annotations

from typing import Any

import httpx
import jwt
from jwt import PyJWK

from coire_core.models.failover import FailoverSnapshot


class AccessIdentityError(ValueError):
    """The Access assertion is missing, expired, or cannot be verified."""


async def verify_access_assertion(
    assertion: str | None,
    snapshot: FailoverSnapshot,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Verify one edge-provided Access JWT using only public, signed snapshot material."""
    if not assertion:
        raise AccessIdentityError("Access assertion is required")
    verifier = snapshot.access_verifier
    owns_client = client is None
    client = client or httpx.AsyncClient(follow_redirects=False)
    try:
        header = jwt.get_unverified_header(assertion)
        kid = str(header["kid"])
        response = await client.get(verifier.jwks_url, timeout=5.0)
        response.raise_for_status()
        body = response.json()
        keys = body.get("keys") if isinstance(body, dict) else None
        jwk = next(item for item in keys or [] if isinstance(item, dict) and item.get("kid") == kid)
        key = PyJWK.from_dict(jwk, algorithm="RS256")
        claims = jwt.decode(
            assertion,
            key.key,
            algorithms=["RS256"],
            audience=verifier.audience,
            issuer=verifier.issuer,
            options={"require": ["exp", "iss", "aud", "sub", "email"]},
        )
        email = claims.get("email")
        if not isinstance(email, str) or not email.strip():
            raise AccessIdentityError("Access identity has no email")
        claims["email"] = email.strip().casefold()
        return claims
    except AccessIdentityError:
        raise
    except (httpx.HTTPError, jwt.PyJWTError, KeyError, StopIteration, TypeError, ValueError) as exc:
        raise AccessIdentityError("Access assertion validation failed") from exc
    finally:
        if owns_client:
            await client.aclose()
