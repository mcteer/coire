"""The authentication seam.

Feature 007 closes ADR-0004's transitional authentication exception. Application middleware
verifies Cloudflare Access assertions or database-backed Coire API keys before routing, while
route dependencies enforce roles and scopes. The legacy static administrator remains available
only behind an explicit, production-disabled emergency compatibility setting.

One rule holds the whole thing up: an **empty** configured token matches nothing. An unset
secret must make nobody an admin, never everybody.
"""

from __future__ import annotations

import hmac
import logging
import uuid
from contextvars import ContextVar
from enum import StrEnum
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from opentelemetry import metrics, trace
from pydantic import BaseModel, ConfigDict

from coire_core.models.auth import ActorType, UserRole

logger = logging.getLogger(__name__)
meter = metrics.get_meter("coire.api.auth")
tracer = trace.get_tracer("coire.api.auth")
auth_attempts = meter.create_counter("coire_auth_attempts_total", unit="1")
auth_audit_failures = meter.create_counter("coire_auth_audit_failures_total", unit="1")

# auto_error=False: a missing header must reach our own handler so the caller gets a 403 with
# an audit row, not FastAPI's bare 403 with no record.
_bearer = HTTPBearer(auto_error=False)


class PrincipalKind(StrEnum):
    ANONYMOUS = "anonymous"
    ADMIN = "admin"
    USER = "user"
    SERVICE = "service"
    API_KEY = "api_key"
    RUN = "run"


class Principal(BaseModel):
    """The request-local identity after independent application verification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: PrincipalKind = PrincipalKind.ANONYMOUS
    subject: str | None = None
    scopes: frozenset[str] = frozenset()
    user_id: uuid.UUID | None = None
    role: UserRole | None = None
    entitlements: frozenset[str] = frozenset()
    api_key_id: uuid.UUID | None = None
    credential_version: int | None = None
    run_id: uuid.UUID | None = None
    permitted_model_ids: frozenset[uuid.UUID] = frozenset()
    permitted_tools: frozenset[str] = frozenset()
    spend_limit_tokens: int | None = None
    spent_tokens: int = 0

    @property
    def is_admin(self) -> bool:
        """Whether this verified principal may use administrative routes."""
        return (
            self.kind is PrincipalKind.ADMIN
            or self.role is UserRole.ADMIN
            or "admin" in self.scopes
        )


ANONYMOUS = Principal()
ADMIN = Principal(kind=PrincipalKind.ADMIN, subject="admin-token")
"""Emergency compatibility principal, enabled only by `identity_legacy_admin_enabled`."""
_current_principal: ContextVar[Principal | None] = ContextVar("coire_principal", default=None)
_current_request_id: ContextVar[uuid.UUID | None] = ContextVar("coire_request_id", default=None)


def bind_principal(principal: Principal):  # type: ignore[no-untyped-def]
    return _current_principal.set(principal)


def reset_principal(token) -> None:  # type: ignore[no-untyped-def]
    _current_principal.reset(token)


def bind_request_id(request_id: uuid.UUID):  # type: ignore[no-untyped-def]
    return _current_request_id.set(request_id)


def reset_request_id(token) -> None:  # type: ignore[no-untyped-def]
    _current_request_id.reset(token)


def current_request_id() -> uuid.UUID | None:
    return _current_request_id.get()


def bound_principal() -> Principal:
    """Return the principal already verified by the application middleware."""
    return _current_principal.get() or ANONYMOUS


def audit_actor(principal: Principal) -> tuple[str, ActorType, uuid.UUID | None]:
    """Project a verified principal into the stable audit actor fields."""
    actor_type = {
        PrincipalKind.USER: ActorType.USER,
        PrincipalKind.API_KEY: ActorType.API_KEY,
        PrincipalKind.ANONYMOUS: ActorType.ANONYMOUS,
    }.get(principal.kind, ActorType.SERVICE)
    return principal.subject or principal.kind.value, actor_type, principal.user_id


def require_scope(scope: str):  # type: ignore[no-untyped-def]
    async def guard(principal: CurrentAuthenticated) -> Principal:
        if principal.kind is not PrincipalKind.API_KEY or scope in principal.scopes:
            return principal
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"API key requires {scope} scope",
        )

    return guard


def principal_from_bearer(
    credentials: HTTPAuthorizationCredentials | None, *, expected: str
) -> Principal:
    """Resolve the emergency compatibility bearer without global configuration."""
    presented = credentials.credentials if credentials else ""
    if expected and presented and hmac.compare_digest(expected, presented):
        return ADMIN
    return ANONYMOUS


async def require_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_api_key: Annotated[str | None, Header(alias="x-api-key")] = None,
) -> Principal:
    """Return the middleware-verified caller, with a disabled-by-default legacy bridge."""
    from coire_core.settings import get_settings

    bound = _current_principal.get()
    if bound is not None:
        return bound
    settings = get_settings()
    expected = (
        settings.admin_token.get_secret_value() if settings.identity_legacy_admin_enabled else ""
    )
    if credentials is None and x_api_key:
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=x_api_key)
    return principal_from_bearer(credentials, expected=expected)


async def authenticate_request(request: Request) -> Principal:
    """Verify one HTTP request without trusting the edge or caching credential success."""
    from sqlalchemy import select

    from coire_api.db import EntitlementRow, UserRow, session_scope
    from coire_api.identity.access import AccessAssertionError, AccessVerifier
    from coire_api.identity.keys import InvalidApiKey, authenticate_key
    from coire_api.identity.limits import enforce_limits
    from coire_core.settings import get_settings

    settings = getattr(request.app.state, "settings", None) or get_settings()
    authorization = request.headers.get("authorization", "")
    scheme, _, bearer = authorization.partition(" ")
    if scheme.casefold() != "bearer":
        bearer = request.headers.get("x-api-key", "")
    if settings.identity_legacy_admin_enabled:
        legacy = principal_from_bearer(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=bearer) if bearer else None,
            expected=settings.admin_token.get_secret_value(),
        )
        if legacy is not ANONYMOUS:
            auth_attempts.add(1, {"method": "legacy", "outcome": "accepted", "reason": "none"})
            return legacy
    assertion = request.headers.get("cf-access-jwt-assertion", "")
    if not bearer.startswith(("coire_", "coire_run_")) and not assertion:
        return ANONYMOUS
    async with session_scope() as session:
        if bearer.startswith("coire_run_"):
            from coire_api.run_tokens import InvalidRunToken, authenticate_run_token

            try:
                run_principal = await authenticate_run_token(session, bearer)
            except InvalidRunToken:
                auth_attempts.add(
                    1, {"method": "run_token", "outcome": "refused", "reason": "invalid"}
                )
                return ANONYMOUS
            auth_attempts.add(1, {"method": "run_token", "outcome": "accepted", "reason": "none"})
            return run_principal
        if bearer.startswith("coire_"):
            try:
                with tracer.start_as_current_span("coire.auth.verify_api_key"):
                    api_principal = await authenticate_key(session, bearer)
                    if api_principal.api_key_id is not None:
                        with tracer.start_as_current_span("coire.auth.enforce_limits"):
                            await enforce_limits(session, api_principal.api_key_id)
            except (InvalidApiKey, LookupError):
                auth_attempts.add(
                    1, {"method": "api_key", "outcome": "refused", "reason": "invalid"}
                )
                return ANONYMOUS
            auth_attempts.add(1, {"method": "api_key", "outcome": "accepted", "reason": "none"})
            return Principal(
                kind=PrincipalKind.API_KEY,
                subject=api_principal.subject,
                user_id=api_principal.user_id,
                role=api_principal.role,
                scopes=frozenset(scope.value for scope in api_principal.scopes),
                entitlements=api_principal.entitlements,
                api_key_id=api_principal.api_key_id,
                credential_version=api_principal.credential_version,
            )
        try:
            verifier = getattr(request.app.state, "access_verifier", None) or AccessVerifier(
                settings
            )
            with tracer.start_as_current_span("coire.auth.verify_access"):
                claims = await verifier.verify(assertion)
        except AccessAssertionError:
            auth_attempts.add(1, {"method": "access", "outcome": "refused", "reason": "invalid"})
            return ANONYMOUS
        user = await session.scalar(
            select(UserRow).where(UserRow.email == claims["email"], UserRow.active.is_(True))
        )
        if user is None:
            auth_attempts.add(
                1, {"method": "access", "outcome": "refused", "reason": "unmatched_user"}
            )
            return ANONYMOUS
        entitlements = frozenset(
            (
                await session.scalars(
                    select(EntitlementRow.name).where(
                        EntitlementRow.user_id == user.id,
                        EntitlementRow.revoked_at.is_(None),
                    )
                )
            ).all()
        )
        auth_attempts.add(1, {"method": "access", "outcome": "accepted", "reason": "none"})
        return Principal(
            kind=PrincipalKind.ADMIN if user.role is UserRole.ADMIN else PrincipalKind.USER,
            subject=str(user.id),
            user_id=user.id,
            role=user.role,
            entitlements=entitlements,
        )


async def audit_authentication_failure(request: Request, *, reason: str) -> None:
    """Best-effort refusal evidence that never persists a presented credential."""
    from coire_api.audit import write_audit
    from coire_api.db import session_scope
    from coire_core.models.audit import AuditOutcome
    from coire_core.models.auth import ActorType

    try:
        async with session_scope() as session:
            await write_audit(
                session,
                actor="anonymous",
                actor_type=ActorType.ANONYMOUS,
                action="authentication.refused",
                target_type="route",
                target_id=f"{request.method} {request.url.path}",
                outcome=AuditOutcome.REFUSED,
                context={
                    "method": request.method,
                    "path": request.url.path,
                    "reason": reason,
                    "client": request.client.host if request.client else "unknown",
                },
            )
    except Exception:
        auth_audit_failures.add(1, {"reason": "database"})
        logger.exception(
            "authentication refusal audit failed method=%s path=%s reason=%s",
            request.method,
            request.url.path,
            reason,
        )


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
