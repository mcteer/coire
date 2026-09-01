"""Administrative local-user, API-key, entitlement, and audit routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Response, status

from coire_api.auth import CurrentAdmin, audit_actor
from coire_api.deps import SessionDep
from coire_api.identity import entitlements, keys, users
from coire_core.models.auth import (
    ApiKey,
    ApiKeyCreate,
    ApiKeyIssued,
    ApiKeyUpdate,
    User,
    UserCreate,
    UserUpdate,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin: identity"])


@router.get("/users", response_model=list[User])
async def list_users(principal: CurrentAdmin, session: SessionDep) -> list[User]:
    return await users.list_users(session)


@router.post("/users", response_model=User, status_code=status.HTTP_201_CREATED)
async def create_user(request: UserCreate, principal: CurrentAdmin, session: SessionDep) -> User:
    actor, actor_type, actor_user_id = audit_actor(principal)
    try:
        result = await users.create_user(
            session,
            request,
            actor=actor,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
        )
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise


@router.patch("/users/{user_id}", response_model=User)
async def update_user(
    user_id: uuid.UUID,
    request: UserUpdate,
    principal: CurrentAdmin,
    session: SessionDep,
) -> User:
    actor, actor_type, actor_user_id = audit_actor(principal)
    try:
        result = await users.update_user(
            session,
            user_id,
            request,
            actor=actor,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
        )
        await session.commit()
        return result
    except users.UserNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found") from exc
    except users.LastAdminError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_user(
    user_id: uuid.UUID,
    principal: CurrentAdmin,
    session: SessionDep,
) -> Response:
    actor, actor_type, actor_user_id = audit_actor(principal)
    try:
        await users.deactivate_user(
            session,
            user_id,
            actor=actor,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
        )
        await session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except users.UserNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found") from exc
    except users.LastAdminError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/users/{user_id}/keys", response_model=list[ApiKey])
async def list_keys(
    user_id: uuid.UUID, principal: CurrentAdmin, session: SessionDep
) -> list[ApiKey]:
    return await keys.list_keys(session, user_id)


@router.post(
    "/users/{user_id}/keys", response_model=ApiKeyIssued, status_code=status.HTTP_201_CREATED
)
async def create_key(
    user_id: uuid.UUID,
    request: ApiKeyCreate,
    principal: CurrentAdmin,
    session: SessionDep,
) -> ApiKeyIssued:
    actor, actor_type, actor_user_id = audit_actor(principal)
    try:
        result = await keys.issue_key(
            session,
            user_id,
            request,
            actor=actor,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
        )
        await session.commit()
        return result
    except keys.KeyNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "active key owner not found") from exc


@router.post("/keys/{key_id}/rotate", response_model=ApiKeyIssued)
async def rotate_key(
    key_id: uuid.UUID, principal: CurrentAdmin, session: SessionDep
) -> ApiKeyIssued:
    actor, actor_type, actor_user_id = audit_actor(principal)
    try:
        result = await keys.rotate_key(
            session,
            key_id,
            actor=actor,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
        )
        await session.commit()
        return result
    except keys.KeyNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "active API key not found") from exc


@router.patch("/keys/{key_id}", response_model=ApiKey)
async def update_key(
    key_id: uuid.UUID,
    request: ApiKeyUpdate,
    principal: CurrentAdmin,
    session: SessionDep,
) -> ApiKey:
    actor, actor_type, actor_user_id = audit_actor(principal)
    try:
        result = await keys.update_key(
            session,
            key_id,
            request,
            actor=actor,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
        )
        await session.commit()
        return result
    except keys.KeyNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "active API key not found") from exc


@router.delete("/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_key(key_id: uuid.UUID, principal: CurrentAdmin, session: SessionDep) -> Response:
    actor, actor_type, actor_user_id = audit_actor(principal)
    try:
        await keys.revoke_key(
            session,
            key_id,
            actor=actor,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
        )
        await session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except keys.KeyNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found") from exc


@router.put("/users/{user_id}/entitlements/{name}", response_model=User)
async def grant_entitlement(
    user_id: uuid.UUID,
    name: str,
    principal: CurrentAdmin,
    session: SessionDep,
) -> User:
    if principal.user_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "local administrator required")
    actor, actor_type, actor_user_id = audit_actor(principal)
    try:
        await entitlements.grant(
            session,
            user_id,
            name,
            granted_by=principal.user_id,
            actor=actor,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
        )
        result = await users.project_user(session, await users.get_user(session, user_id))
        await session.commit()
        return result
    except entitlements.EntitlementUserNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found") from exc


@router.delete("/users/{user_id}/entitlements/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_entitlement(
    user_id: uuid.UUID,
    name: str,
    principal: CurrentAdmin,
    session: SessionDep,
) -> Response:
    actor, actor_type, actor_user_id = audit_actor(principal)
    changed = await entitlements.revoke(
        session,
        user_id,
        name,
        actor=actor,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
    )
    if not changed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "active entitlement not found")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
