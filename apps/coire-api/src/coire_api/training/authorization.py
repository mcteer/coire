"""User-bound training authority with fresh checks and content-free refusal audit."""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import Depends, Request
from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import CurrentPrincipal, Principal, PrincipalKind
from coire_api.db import ApiKeyRow, UserRow, session_scope
from coire_core.errors import TrainingForbidden, TrainingUnavailable
from coire_core.models.audit import AuditOutcome
from coire_core.models.auth import UserRole
from coire_core.settings import get_settings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.api.training")
_HUMAN = frozenset({PrincipalKind.USER, PrincipalKind.ADMIN})
_READ = frozenset({"GET", "HEAD", "OPTIONS"})


def preflight_training_action(
    principal: Principal, *, method: str, origin: str | None, browser_origin: str
) -> uuid.UUID:
    owner = principal.user_id
    if owner is None or principal.kind not in (_HUMAN | {PrincipalKind.API_KEY}):
        raise TrainingForbidden()
    if not principal.is_admin:
        raise TrainingForbidden()
    if principal.kind is PrincipalKind.API_KEY:
        if (
            principal.api_key_id is None
            or principal.credential_version is None
            or "admin" not in principal.scopes
        ):
            raise TrainingForbidden()
    elif method not in _READ and (not browser_origin or origin != browser_origin):
        raise TrainingForbidden()
    return owner


async def authorize_live_training_action(
    session: AsyncSession, principal: Principal, *, shared: bool = False
) -> uuid.UUID:
    """Block privilege mutation through the transaction; exclusive by default.

    Readonly guards may share barriers with each other. They must not upgrade or
    modify identity rows; every privilege mutation still conflicts with FOR SHARE.
    """
    with tracer.start_as_current_span("coire.api.training.authorize"):
        owner = preflight_training_action(principal, method="GET", origin=None, browser_origin="")
        lock: bool | dict[str, bool] = {"read": True} if shared else True
        user = await session.get(UserRow, owner, populate_existing=True, with_for_update=lock)
        if user is None or not user.active or user.role is not UserRole.ADMIN:
            raise TrainingForbidden()
        if principal.kind is PrincipalKind.API_KEY:
            key = await session.get(
                ApiKeyRow, principal.api_key_id, populate_existing=True, with_for_update=lock
            )
            if (
                key is None
                or key.user_id != owner
                or key.revoked_at is not None
                or key.credential_version != principal.credential_version
                or "admin" not in key.scopes
            ):
                raise TrainingForbidden()
        return owner


async def require_training_principal(request: Request, principal: CurrentPrincipal) -> Principal:
    settings = getattr(request.app.state, "settings", None) or get_settings()
    try:
        preflight_training_action(
            principal,
            method=request.method,
            origin=request.headers.get("origin"),
            browser_origin=settings.chat_browser_origin,
        )
        async with session_scope() as session:
            await authorize_live_training_action(session, principal)
    except TrainingForbidden:
        # A denied privileged action has its own transaction; it cannot claim a mutation.
        try:
            async with session_scope() as session:
                await write_principal_audit(
                    session,
                    principal=principal,
                    action="training.refused",
                    target_type="route",
                    target_id=f"{request.method} {request.url.path}",
                    outcome=AuditOutcome.REFUSED,
                    context={"reason": "authorization"},
                )
                await session.commit()
        except Exception as error:
            logger.error(
                "training refusal audit unavailable",
                extra={"operation": "authorize", "safe_reason": "audit_unavailable"},
            )
            raise TrainingUnavailable("Training authorization audit is unavailable") from error
        raise
    return principal


CurrentTrainingAdmin = Annotated[Principal, Depends(require_training_principal)]
