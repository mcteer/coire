"""The authentication seam.

Feature 000 shipped **no** authentication on control-plane routes — a time-boxed exception to
Principle IV recorded in ADR-0001. Feature 001 adds the first routes that must refuse a
non-admin, so it adds the smallest real credential that can do that: a static admin bearer
token from core's Keychain, mounted as a file secret (**ADR-0004**).

This is still not authentication in the sense Principle IV means, and it is deliberately not a
placeholder that always succeeds — a check that cannot fail looks like protection and is worse
than none. Feature 007 replaces `require_principal`'s body with edge-identity assertion and
API-key validation; no route signature changes, because every route already depends on it.

One rule holds the whole thing up: an **empty** configured token matches nothing. An unset
secret must make nobody an admin, never everybody.
"""

from __future__ import annotations

import hmac
import logging
from enum import StrEnum
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

# auto_error=False: a missing header must reach our own handler so the caller gets a 403 with
# an audit row, not FastAPI's bare 403 with no record.
_bearer = HTTPBearer(auto_error=False)


class PrincipalKind(StrEnum):
    ANONYMOUS = "anonymous"
    ADMIN = "admin"  # ADR-0004; feature 007 replaces this with real roles
    USER = "user"  # issued by feature 007
    SERVICE = "service"  # issued by feature 007


class Principal(BaseModel):
    """Who is making a request. Until feature 007 this is always anonymous."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: PrincipalKind = PrincipalKind.ANONYMOUS
    subject: str | None = None
    scopes: frozenset[str] = frozenset()

    @property
    def is_admin(self) -> bool:
        """Only the ADR-0004 admin token yields this. Real roles arrive with feature 007."""
        return self.kind is PrincipalKind.ADMIN


ANONYMOUS = Principal()
ADMIN = Principal(kind=PrincipalKind.ADMIN, subject="admin-token")
"""The interim admin. `subject` is the literal string the audit log records until feature 007
supplies real identities — that is the truthful account of this period, not a placeholder."""


def principal_from_bearer(
    credentials: HTTPAuthorizationCredentials | None, *, expected: str
) -> Principal:
    """Resolve the interim bearer without consulting global configuration."""
    presented = credentials.credentials if credentials else ""
    if expected and presented and hmac.compare_digest(expected, presented):
        return ADMIN
    return ANONYMOUS


async def require_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_api_key: Annotated[str | None, Header(alias="x-api-key")] = None,
) -> Principal:
    """Resolve the caller.

    Feature 007 replaces this body with Cloudflare Access assertion validation and API-key
    lookup. Until then there is exactly one credential: the admin token (ADR-0004). Everyone
    else — no header, wrong token — is anonymous, and the platform is reachable only on core's
    loopback and the unrouted mesh.
    """
    from coire_core.settings import get_settings

    expected = get_settings().admin_token.get_secret_value()
    if credentials is None and x_api_key:
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=x_api_key)
    return principal_from_bearer(credentials, expected=expected)


CurrentPrincipal = Annotated[Principal, Depends(require_principal)]


async def require_authenticated(principal: CurrentPrincipal) -> Principal:
    """Require a credential the platform has actually verified."""
    if principal.kind is not PrincipalKind.ANONYMOUS:
        return principal
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="valid bearer credential required",
        headers={"WWW-Authenticate": "Bearer"},
    )


CurrentAuthenticated = Annotated[Principal, Depends(require_authenticated)]


async def require_admin(request: Request, principal: CurrentPrincipal) -> Principal:
    """Guard every `/api/v1/admin/*` route.

    A refusal writes its own audit row in its own session and commits it: the request is about
    to be abandoned, so there is no route transaction to attach to, and SC-002 is verified by
    reading the audit log rather than by observing that nothing changed.
    """
    if principal.is_admin:
        return principal

    from coire_api.audit import write_audit
    from coire_api.db import session_scope
    from coire_core.models.audit import AuditOutcome

    logger.warning(
        "refused non-admin %s %s from %s",
        request.method,
        request.url.path,
        request.client.host if request.client else "unknown",
    )
    try:
        async with session_scope() as session:
            await write_audit(
                session,
                actor=principal.kind.value,
                action="admin.refused",
                target_type="route",
                target_id=f"{request.method} {request.url.path}",
                outcome=AuditOutcome.REFUSED,
            )
    except Exception:  # pragma: no cover - a failed audit must not mask the refusal
        logger.exception("could not write the refusal audit row")

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="admin credential required",
    )


CurrentAdmin = Annotated[Principal, Depends(require_admin)]
