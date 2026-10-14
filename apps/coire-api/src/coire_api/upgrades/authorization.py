"""Live human-admin initiation; internal execution cannot widen accepted authority."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import CurrentPrincipal, Principal, PrincipalKind
from coire_api.db import session_scope
from coire_api.training.authorization import (
    authorize_live_training_action,
    preflight_training_action,
)
from coire_core.errors import TrainingForbidden, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.audit import AuditOutcome
from coire_core.settings import get_settings


async def authorize(session: AsyncSession, principal: Principal) -> UUID:
    try:
        return await authorize_live_training_action(session, principal)
    except TrainingForbidden:
        raise UpgradeForbidden() from None


async def require_upgrade_admin(request: Request, principal: CurrentPrincipal) -> Principal:
    settings = getattr(request.app.state, "settings", None) or get_settings()
    try:
        preflight_training_action(
            principal,
            method=request.method,
            origin=request.headers.get("origin"),
            browser_origin=settings.chat_browser_origin,
        )
        async with session_scope() as session:
            await authorize(session, principal)
    except (TrainingForbidden, UpgradeForbidden):
        try:
            async with session_scope() as session:
                await write_principal_audit(
                    session,
                    principal=principal,
                    action="upgrades.refused",
                    target_type="route",
                    target_id=f"{request.method} {request.url.path}",
                    outcome=AuditOutcome.REFUSED,
                    context={"reason": "authorization"},
                )
                await session.commit()
        except Exception as error:
            raise UpgradeUnavailable("Upgrade authorization audit is unavailable") from error
        raise UpgradeForbidden() from None
    return principal


async def require_upgrade_executor(request: Request, principal: CurrentPrincipal) -> Principal:
    settings = getattr(request.app.state, "settings", None) or get_settings()
    if (
        principal.kind is not PrincipalKind.UPGRADE_EXECUTOR
        or principal.subject != settings.upgrade_executor_id
        or principal.scopes != frozenset({"upgrades:execute"})
    ):
        raise UpgradeForbidden()
    return principal


UpgradeAdmin = Annotated[Principal, Depends(require_upgrade_admin)]
UpgradeExecutor = Annotated[Principal, Depends(require_upgrade_executor)]
