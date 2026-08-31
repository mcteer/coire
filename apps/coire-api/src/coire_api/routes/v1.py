"""OpenAI-compatible `/v1` gateway routes."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress
from time import perf_counter
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import Receive, Scope, Send

from coire_api.auth import CurrentPrincipal, Principal
from coire_api.db import EngineProcessRow, ModelRow
from coire_api.deps import SessionDep, SettingsDep
from coire_api.gateway.anthropic import from_openai_response, from_openai_stream, to_openai_payload
from coire_api.gateway.context import ContextLengthError, enforce_context
from coire_api.gateway.loading import ModelLoadError, load_model
from coire_api.gateway.proxy import (
    EngineProxyError,
    EngineSaturatedError,
    StreamTiming,
    complete,
    stream,
)
from coire_api.gateway.resolution import ModelNotFoundError, ResolvedModel, resolve_model
from coire_api.gateway.telemetry import first_token_duration_ms, overhead_duration_ms
from coire_api.gateway.usage import UsageTracker
from coire_api.registry.service import load_state_for, visible_to
from coire_core.models.gateway import (
    AnthropicMessagesRequest,
    ChatCompletionRequest,
    GatewayModel,
    GatewayModelList,
    GatewayProtocol,
    UsageOutcome,
)
from coire_core.models.registry import LoadState
from coire_core.settings import Settings

router = APIRouter(prefix="/v1", tags=["compatible"])
logger = logging.getLogger(__name__)


async def _finish_detached(
    usage: UsageTracker, outcome: UsageOutcome, *, failure_code: str
) -> None:
    task = asyncio.create_task(usage.finish(outcome, failure_code=failure_code))
    # The detached task must outlive an ASGI cancel scope long enough to commit.
    with suppress(asyncio.CancelledError):
        await asyncio.shield(task)


class _UsageStreamingResponse(StreamingResponse):
    def __init__(self, source: AsyncIterator[bytes], usage: UsageTracker) -> None:
        super().__init__(
            source,
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )
        self._usage = usage

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await _finish_detached(
                self._usage,
                UsageOutcome.DISCONNECTED,
                failure_code="client_disconnected",
            )


async def _load_and_resolve(
    body_model: uuid.UUID, principal: Principal, session: AsyncSession, settings: Settings
) -> ResolvedModel:
    await asyncio.wait_for(
        load_model(body_model, settings), timeout=settings.gateway_wait_ceiling_s
    )
    session.expire_all()
    return await resolve_model(session, body_model, principal)


async def _openai_cold_stream(
    body: ChatCompletionRequest,
    principal: Principal,
    session: AsyncSession,
    settings: Settings,
    usage: UsageTracker,
    request: Request,
    timing: StreamTiming,
) -> AsyncIterator[bytes]:
    task = asyncio.create_task(_load_and_resolve(body.model, principal, session, settings))
    while not task.done():
        try:
            resolved = await asyncio.wait_for(
                asyncio.shield(task), timeout=settings.gateway_keepalive_interval_s
            )
            break
        except TimeoutError:
            yield b": coire model loading\n\n"
    try:
        resolved = await task
        if resolved.engine_url is None or resolved.model_path is None:
            raise ModelLoadError("engine did not become ready")
        usage.model_id = resolved.model_id
        usage.engine_id = resolved.engine_id
        payload = body.model_dump(mode="json", exclude={"coire_wait_for_model"}, exclude_none=True)
        payload["model"] = resolved.model_path
        async for chunk in _tracked_stream(
            stream(resolved.engine_url, payload, settings, timing), usage, request, timing
        ):
            yield chunk
    except (ModelLoadError, TimeoutError) as exc:
        await usage.finish(UsageOutcome.FAILED, failure_code="model_load_failed")
        error = json.dumps({"error": {"message": str(exc), "type": "model_load_error"}})
        yield f"data: {error}\n\n".encode()


async def _tracked_stream(
    source: AsyncIterator[bytes],
    usage: UsageTracker,
    request: Request | None = None,
    timing: StreamTiming | None = None,
) -> AsyncIterator[bytes]:
    first_observed = False
    try:
        async for chunk in source:
            if request is not None and await request.is_disconnected():
                await usage.finish(UsageOutcome.DISCONNECTED, failure_code="client_disconnected")
                return
            if (
                not first_observed
                and timing is not None
                and timing.upstream_started_at is not None
                and timing.first_chunk_at is not None
            ):
                first_observed = True
                first_token_ms = (perf_counter() - timing.request_started_at) * 1000
                engine_ms = (timing.first_chunk_at - timing.upstream_started_at) * 1000
                overhead_ms = max(first_token_ms - engine_ms, 0)
                attributes = {"protocol": usage.protocol.value}
                first_token_duration_ms.record(first_token_ms, attributes)
                overhead_duration_ms.record(overhead_ms, attributes)
                logger.info(
                    "gateway first token request_id=%s model_id=%s engine_id=%s "
                    "first_token_ms=%.2f gateway_overhead_ms=%.2f",
                    usage.request_id,
                    usage.model_id,
                    usage.engine_id,
                    first_token_ms,
                    overhead_ms,
                )
            for line in chunk.decode(errors="replace").splitlines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                try:
                    event = json.loads(line[6:])
                    reported = event.get("usage") or {}
                    if reported:
                        usage.prompt_tokens = int(
                            reported.get("prompt_tokens", usage.prompt_tokens)
                        )
                        usage.completion_tokens = int(
                            reported.get("completion_tokens", usage.completion_tokens)
                        )
                    elif event.get("choices", [{}])[0].get("delta", {}).get("content"):
                        usage.completion_tokens += 1
                except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                    pass
            yield chunk
    except asyncio.CancelledError:
        await _finish_detached(usage, UsageOutcome.DISCONNECTED, failure_code="client_disconnected")
        raise
    except EngineProxyError:
        await usage.finish(UsageOutcome.FAILED, failure_code="engine_stream_failed")
    else:
        await usage.finish(UsageOutcome.SUCCEEDED)


def _streaming_response(source: AsyncIterator[bytes], usage: UsageTracker) -> StreamingResponse:
    """Finalize accounting when the ASGI server closes an abandoned response."""

    return _UsageStreamingResponse(source, usage)


async def _anthropic_cold_stream(
    body: AnthropicMessagesRequest,
    principal: Principal,
    session: AsyncSession,
    settings: Settings,
    usage: UsageTracker,
    request: Request,
    timing: StreamTiming,
) -> AsyncIterator[bytes]:
    task = asyncio.create_task(_load_and_resolve(body.model, principal, session, settings))
    while not task.done():
        try:
            resolved = await asyncio.wait_for(
                asyncio.shield(task), timeout=settings.gateway_keepalive_interval_s
            )
            break
        except TimeoutError:
            yield b": coire model loading\n\n"
    try:
        resolved = await task
        if resolved.engine_url is None or resolved.model_path is None:
            raise ModelLoadError("engine did not become ready")
        usage.model_id = resolved.model_id
        usage.engine_id = resolved.engine_id
        payload = to_openai_payload(body, model_path=resolved.model_path)
        tracked = _tracked_stream(
            stream(resolved.engine_url, payload, settings, timing), usage, request, timing
        )
        async for event in from_openai_stream(tracked, model=body.model):
            yield event
    except (ModelLoadError, TimeoutError) as exc:
        await usage.finish(UsageOutcome.FAILED, failure_code="model_load_failed")
        yield (
            "event: error\ndata: "
            + json.dumps(
                {"type": "error", "error": {"type": "model_load_error", "message": str(exc)}}
            )
            + "\n\n"
        ).encode()


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
    request: Request,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> object:
    usage = UsageTracker(principal, str(body.model), GatewayProtocol.OPENAI)
    timing = StreamTiming()
    try:
        resolved = await resolve_model(session, body.model, principal)
    except ModelNotFoundError as exc:
        await usage.finish(UsageOutcome.REFUSED, failure_code="model_not_found")
        raise HTTPException(status.HTTP_404_NOT_FOUND, "model not found") from exc
    usage.model_id = resolved.model_id
    usage.engine_id = resolved.engine_id
    try:
        usage.prompt_tokens = enforce_context(
            body.messages, limit=resolved.context_window, output_tokens=body.max_tokens or 0
        )
    except ContextLengthError as exc:
        await usage.finish(UsageOutcome.REFUSED, failure_code="context_length")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if resolved.engine_url is None or resolved.model_path is None:
        if not body.coire_wait_for_model:
            await usage.finish(UsageOutcome.REFUSED, failure_code="model_cold")
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "model is not loaded",
                headers={"Retry-After": str(settings.gateway_retry_after_s)},
            )
        if body.stream:
            return _streaming_response(
                _openai_cold_stream(body, principal, session, settings, usage, request, timing),
                usage,
            )
        try:
            resolved = await _load_and_resolve(body.model, principal, session, settings)
        except (ModelLoadError, TimeoutError) as exc:
            await usage.finish(UsageOutcome.FAILED, failure_code="model_load_failed")
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"model load failed: {exc}",
                headers={"Retry-After": str(settings.gateway_retry_after_s)},
            ) from exc
        if resolved.engine_url is None or resolved.model_path is None:
            await usage.finish(UsageOutcome.FAILED, failure_code="model_not_ready")
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "model load did not become ready"
            )
    payload = body.model_dump(mode="json", exclude={"coire_wait_for_model"}, exclude_none=True)
    payload["model"] = resolved.model_path
    try:
        if body.stream:
            return _streaming_response(
                _tracked_stream(
                    stream(resolved.engine_url, payload, settings, timing), usage, request, timing
                ),
                usage,
            )
        result = await complete(resolved.engine_url, payload, settings)
        reported = result.get("usage")
        if isinstance(reported, dict):
            usage.prompt_tokens = int(reported.get("prompt_tokens", usage.prompt_tokens))
            usage.completion_tokens = int(reported.get("completion_tokens", 0))
        await usage.finish(UsageOutcome.SUCCEEDED)
        return result
    except EngineSaturatedError as exc:
        await usage.finish(UsageOutcome.REFUSED, failure_code="engine_saturated")
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "engine is saturated",
            headers={"Retry-After": "1"},
        ) from exc
    except EngineProxyError as exc:
        await usage.finish(UsageOutcome.FAILED, failure_code="engine_request_failed")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "engine request failed") from exc


@router.post("/messages")
async def anthropic_messages(
    body: AnthropicMessagesRequest,
    request: Request,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> object:
    usage = UsageTracker(principal, str(body.model), GatewayProtocol.ANTHROPIC)
    timing = StreamTiming()
    try:
        resolved = await resolve_model(session, body.model, principal)
    except ModelNotFoundError as exc:
        await usage.finish(UsageOutcome.REFUSED, failure_code="model_not_found")
        raise HTTPException(status.HTTP_404_NOT_FOUND, "model not found") from exc
    usage.model_id = resolved.model_id
    usage.engine_id = resolved.engine_id
    if resolved.engine_url is None or resolved.model_path is None:
        if not body.coire_wait_for_model:
            await usage.finish(UsageOutcome.REFUSED, failure_code="model_cold")
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "model is not loaded",
                headers={"Retry-After": str(settings.gateway_retry_after_s)},
            )
        if body.stream:
            return _streaming_response(
                _anthropic_cold_stream(body, principal, session, settings, usage, request, timing),
                usage,
            )
        try:
            resolved = await _load_and_resolve(body.model, principal, session, settings)
        except (ModelLoadError, TimeoutError) as exc:
            await usage.finish(UsageOutcome.FAILED, failure_code="model_load_failed")
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"model load failed: {exc}",
                headers={"Retry-After": str(settings.gateway_retry_after_s)},
            ) from exc
        if resolved.engine_url is None or resolved.model_path is None:
            await usage.finish(UsageOutcome.FAILED, failure_code="model_not_ready")
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "model load did not become ready"
            )
    payload = to_openai_payload(body, model_path=resolved.model_path)
    try:
        if body.stream:
            source = _tracked_stream(
                stream(resolved.engine_url, payload, settings, timing), usage, request, timing
            )
            return _streaming_response(from_openai_stream(source, model=body.model), usage)
        result = await complete(resolved.engine_url, payload, settings)
        reported = result.get("usage")
        if isinstance(reported, dict):
            usage.prompt_tokens = int(reported.get("prompt_tokens", 0))
            usage.completion_tokens = int(reported.get("completion_tokens", 0))
        await usage.finish(UsageOutcome.SUCCEEDED)
        return from_openai_response(result, model=body.model)
    except EngineSaturatedError as exc:
        await usage.finish(UsageOutcome.REFUSED, failure_code="engine_saturated")
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "engine is saturated", headers={"Retry-After": "1"}
        ) from exc
    except EngineProxyError as exc:
        await usage.finish(UsageOutcome.FAILED, failure_code="engine_request_failed")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "engine request failed") from exc
