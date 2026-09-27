"""Current authenticated local identity."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from coire_api.auth import CurrentAuthenticated
from coire_api.deps import SessionDep
from coire_api.identity.users import get_user, project_user
from coire_core.models.auth import User

router = APIRouter(prefix="/api/v1", tags=["identity"])


@router.get("/me", response_model=User)
async def me(principal: CurrentAuthenticated, session: SessionDep) -> User:
    user_id = getattr(principal, "user_id", None)
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "local user identity required")
    return await project_user(session, await get_user(session, user_id))
