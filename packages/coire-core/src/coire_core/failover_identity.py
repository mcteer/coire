"""Snapshot-approved Access identity verification shared by the origin and node relay."""

from __future__ import annotations

from typing import Any

import httpx
import jwt
from jwt import PyJWK

from coire_core.models.failover import FailoverSnapshot


class AccessIdentityError(ValueError):
    """Missing, expired, unverifiable or ineligible browser identity."""


async def verify_access_assertion(
    assertion: str | None,
    snapshot: FailoverSnapshot,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    if not assertion or len(assertion) > 16384:
        raise AccessIdentityError("Access assertion is required")
    verifier = snapshot.access_verifier
    owns_client = client is None
    client = client or httpx.AsyncClient(follow_redirects=False)
    try:
        header = jwt.get_unverified_header(assertion)
        kid = str(header["kid"])
        response = await client.get(verifier.jwks_url, timeout=1.0)
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
        if not snapshot.is_current():
            raise AccessIdentityError("snapshot authority expired during verification")
        eligible = next(
            (
                subject
                for subject in snapshot.subjects
                if subject.issuer == claims["iss"] and subject.subject == claims["sub"]
            ),
            None,
        )
        if eligible is None or not eligible.eligible or eligible.mutable_budget_required:
            raise AccessIdentityError("Access identity is not eligible for degraded inference")
        claims["email"] = email.strip().casefold()
        return claims
    except AccessIdentityError:
        raise
    except (httpx.HTTPError, jwt.PyJWTError, KeyError, StopIteration, TypeError, ValueError) as exc:
        raise AccessIdentityError("Access assertion validation failed") from exc
    finally:
        if owns_client:
            await client.aclose()
