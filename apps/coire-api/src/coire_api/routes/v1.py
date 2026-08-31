"""OpenAI-compatible `/v1` gateway routes."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from fastapi import APIRouter, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from coire_api.auth import CurrentPrincipal
from coire_api.db import EngineProcessRow, ModelRow
from coire_api.deps import SessionDep, SettingsDep
from coire_api.gateway.context import ContextLengthError, enforce_context
from coire_api.gateway.proxy import EngineProxyError, EngineSaturatedError, complete, stream
from coire_api.gateway.resolution import ModelNotFoundError, resolve_model
from coire_api.registry.service import load_state_for, visible_to
from coire_core.models.gateway import ChatCompletionRequest, GatewayModel, GatewayModelList
from coire_core.models.registry import LoadState

router = APIRouter(prefix="/v1", tags=["compatible"])


def _load_label(state: LoadState) -> Literal["loaded", "loading", "cold"]:
    if state is LoadState.LOADED:
        return "loaded"
    if state is LoadState.LOADING:
        return "loading"
    return "cold"


@router.get("/models", response_model=GatewayModelList)
async def list_models(principal: CurrentPrincipal, session: SessionDep) -> GatewayModelList:
    rows = (await session.execute(select(ModelRow).order_by(ModelRow.display_name))).scalars().all()
    visible = [model for model in rows if visible_to(is_admin=principal.is_admin, model=model)]
    engines: Sequence[EngineProcessRow] = []
    if visible:
        engines = (
            (
                await session.execute(
                    select(EngineProcessRow).where(
                        EngineProcessRow.model_id.in_([model.id for model in visible])
                    )
                )
            )
            .scalars()
            .all()
        )
    by_model: dict[object, list[EngineProcessRow]] = {}
    for engine in engines:
        by_model.setdefault(engine.model_id, []).append(engine)
    return GatewayModelList(
        data=[
            GatewayModel(
                id=model.id,
                created=int(model.created_at.timestamp()),
                coire_load_state=_load_label(load_state_for(by_model.get(model.id, []))[0]),
                coire_tags=model.tags,
                coire_description=model.description,
                coire_context_window=model.context_window,
            )
            for model in visible
        ]
    )


@router.post("/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    response: Response,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> object:
    try:
        resolved = await resolve_model(session, body.model, principal)
    except ModelNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "model not found") from exc
    try:
        enforce_context(
            body.messages, limit=resolved.context_window, output_tokens=body.max_tokens or 0
        )
    except ContextLengthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if resolved.engine_url is None or resolved.model_path is None:
        response.headers["Retry-After"] = str(settings.gateway_retry_after_s)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "model is not loaded")
    payload = body.model_dump(mode="json", exclude={"coire_wait_for_model"}, exclude_none=True)
    payload["model"] = resolved.model_path
    try:
        if body.stream:
            return StreamingResponse(
                stream(resolved.engine_url, payload, settings),
                media_type="text/event-stream",
                headers={"X-Accel-Buffering": "no"},
            )
        return await complete(resolved.engine_url, payload, settings)
    except EngineSaturatedError as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "engine is saturated",
            headers={"Retry-After": "1"},
        ) from exc
    except EngineProxyError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "engine request failed") from exc
