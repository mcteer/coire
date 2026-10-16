"""Explicit local browser login and revocation; anonymous requests obtain no identity data."""

import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import delete

from coire_api.audit import write_audit
from coire_api.db import BrowserSessionRow, LocalCredentialRow, session_scope
from coire_api.identity.local import attempts, cookie_name, digest, require_origin, verify_login
from coire_api.identity.users import project_user
from coire_core.models.audit import AuditOutcome
from coire_core.models.auth import ActorType
from coire_core.models.local_auth import LocalAuthStatus, LocalLogin, LocalSessionIssued

router = APIRouter(prefix="/api/v1/auth/local", tags=["identity"])


@router.get("", response_model=LocalAuthStatus)
async def configuration(request: Request, response: Response) -> LocalAuthStatus:
    response.headers["Cache-Control"] = "no-store"
    settings = request.app.state.settings
    return LocalAuthStatus(
        enabled=bool(settings.local_auth_enabled and settings.local_auth_origin),
        access_enabled=bool(
            settings.cloudflare_access_issuer and settings.cloudflare_access_audience
        ),
    )


@router.post("/login", response_model=LocalSessionIssued)
async def login(body: LocalLogin, request: Request, response: Response) -> LocalSessionIssued:
    settings = request.app.state.settings
    if not settings.local_auth_enabled or not settings.local_auth_origin:
        raise HTTPException(404, "local sign-in is disabled")
    require_origin(request)
    async with session_scope() as session:
        user, limited = await verify_login(session, body.email, body.password.get_secret_value())
        outcome = "limited" if limited else "accepted" if user else "refused"
        attempts.add(1, {"outcome": outcome})
        await write_audit(
            session,
            actor=str(user.id) if user else "anonymous",
            action="identity.local.login",
            actor_type=ActorType.USER if user else ActorType.ANONYMOUS,
            actor_user_id=user.id if user else None,
            target_type="authentication",
            target_id="local",
            outcome=AuditOutcome.OK if user else AuditOutcome.REFUSED,
        )
        if user is None:
            await session.commit()
            raise HTTPException(
                429 if limited else 401,
                "too many sign-in attempts" if limited else "email or password is incorrect",
                headers={"Retry-After": "60"} if limited else None,
            )
        credential = await session.get(LocalCredentialRow, user.id)
        assert credential is not None
        await session.execute(
            delete(BrowserSessionRow).where(BrowserSessionRow.expires_at <= datetime.now(UTC))
        )
        token = secrets.token_urlsafe(32)
        expires = datetime.now(UTC) + timedelta(hours=8)
        session.add(
            BrowserSessionRow(
                digest=digest(token),
                user_id=user.id,
                generation=credential.generation,
                expires_at=expires,
            )
        )
        result = LocalSessionIssued(user=await project_user(session, user), expires_at=expires)
        response.set_cookie(
            cookie_name(settings),
            token,
            max_age=28800,
            httponly=True,
            secure=settings.local_auth_origin.startswith("https://"),
            samesite="strict",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return result


@router.post("/logout", status_code=204)
async def logout(request: Request) -> Response:
    require_origin(request)
    settings = request.app.state.settings
    async with session_scope() as session:
        row = await session.get(
            BrowserSessionRow, digest(request.cookies.get(cookie_name(settings), ""))
        )
        if row is None:
            raise HTTPException(401, "local session required")
        row.revoked_at = datetime.now(UTC)
        await write_audit(
            session,
            actor=str(row.user_id),
            actor_type=ActorType.USER,
            actor_user_id=row.user_id,
            action="identity.local.logout",
            target_type="authentication",
            target_id="local",
        )
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.delete_cookie(
        cookie_name(settings),
        path="/",
        secure=settings.local_auth_origin.startswith("https://"),
        httponly=True,
        samesite="strict",
    )
    return response
