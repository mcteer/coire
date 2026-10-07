"""OpenAI-compatible `/v1` gateway routes."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import CurrentAuthenticated, Principal, PrincipalKind, require_scope
from coire_api.db import EngineProcessRow, ModelRow, TrainingAdapterRow
from coire_api.deps import SessionDep, SettingsDep
from coire_api.gateway.anthropic import (
    from_openai_response,
    from_openai_stream,
    require_anthropic_text,
    to_openai_payload,
)
from coire_api.gateway.context import (
    ContextLengthError,
    VisualContextUnavailable,
    enforce_anthropic_context,
    enforce_context,
)
from coire_api.gateway.execution import (
    cancel_pending_load,
    compatible_text_payload,
)
from coire_api.gateway.execution import (
    rewrite_openai_model as _rewrite_openai_model,
)
from coire_api.gateway.execution import (
    streaming_response as _streaming_response,
)
from coire_api.gateway.execution import (
    track_stream as _tracked_stream,
)
from coire_api.gateway.loading import ModelLoadError, load_model
from coire_api.gateway.proxy import (
    EngineProxyError,
    EngineSaturatedError,
    StreamTiming,
    complete,
    stream,
)
from coire_api.gateway.resolution import (
    ModelNotFoundError,
    ResolvedModel,
    resolve_model,
    retry_after_seconds,
)
from coire_api.gateway.targets import parse_selector, resolve_target
from coire_api.gateway.temporary import (
    TemporaryVisualQuotaExceeded,
    TemporaryVisualUnavailable,
    normalize_inline_images,
)
from coire_api.gateway.text_completions import text_response, text_stream
from coire_api.gateway.usage import UsageTracker
from coire_api.registry.service import load_state_for, visible_to
from coire_core.models.gateway import (
    AnthropicMessagesRequest,
    ChatCompletionRequest,
    ChatMessage,
    GatewayModel,
    GatewayModelList,
    GatewayProtocol,
    TextCompletionRequest,
    TextCompletionResponse,
    UsageOutcome,
)
from coire_core.models.registry import EngineBackend, LoadState, ModelSource
from coire_core.settings import Settings

router = APIRouter(prefix="/v1", tags=["compatible"], dependencies=[Depends(require_scope("chat"))])


async def _load_and_resolve(
    selector: str | uuid.UUID,
    principal: Principal,
    session: AsyncSession,
    settings: Settings,
    affinity_node: str | None = None,
    variant_id: uuid.UUID | None = None,
) -> ResolvedModel:
    selected = await resolve_target(session, selector, principal, variant_id)
    key = selected.identity or selected.model.id
    await session.rollback()
    await asyncio.wait_for(load_model(key, settings), timeout=settings.gateway_wait_ceiling_s)
    session.expire_all()
    if principal.run_id is not None:
        from coire_api.run_tokens import run_token_is_active

        if not await run_token_is_active(session, principal.run_id):
            raise ModelLoadError("run credential revoked")
    return await resolve_model(session, selector, principal, affinity_node, variant_id=variant_id)


def _compatible_payload(body: ChatCompletionRequest, model_path: str) -> dict[str, object]:
    payload = compatible_text_payload(body, model_path)
    payload.pop("coire_variant_id", None)
    return payload


async def _rewrite_selector(
    source: AsyncIterator[bytes],
    selector: uuid.UUID | str,
    *,
    private_model_path: str | None = None,
) -> AsyncIterator[bytes]:
    # The shared helper provides bounded framing and private-path rejection. Until its
    # annotation accepts selectors, pass a UUID and rewrite its complete public frames.
    model_id, slug = parse_selector(selector)
    rewritten = _rewrite_openai_model(source, model_id, private_model_path=private_model_path)
    try:
        async for frame in rewritten:
            if slug is None:
                yield frame
                continue
            lines = frame.rstrip(b"\n").split(b"\n")
            data = b"\n".join(
                line[5:].removeprefix(b" ") for line in lines if line.startswith(b"data:")
            )
            if not data or data == b"[DONE]":
                yield frame
                continue
            event = json.loads(data)
            if not isinstance(event, dict) or "model" not in event:
                yield frame
                continue
            event["model"] = str(selector)
            metadata = [line for line in lines if not line.startswith(b"data:")]
            yield (
                b"\n".join(
                    [*metadata, b"data: " + json.dumps(event, separators=(",", ":")).encode()]
                )
                + b"\n\n"
            )
    finally:
        close = getattr(rewritten, "aclose", None)
        if close is not None:
            await close()


def _tool_names(tools: list[dict[str, Any]] | None) -> set[str]:
    names: set[str] = set()
    for tool in tools or []:
        function = tool.get("function")
        name = function.get("name") if isinstance(function, dict) else tool.get("name")
        if isinstance(name, str):
            names.add(name)
    return names


def _enforce_run_request_scope(
    principal: Principal,
    *,
    tools: list[dict[str, Any]] | None,
    prompt_tokens: int,
    max_tokens: int | None,
) -> int | None:
    if principal.kind is not PrincipalKind.RUN:
        return max_tokens
    requested_tools = _tool_names(tools)
    if not requested_tools.issubset(principal.permitted_tools):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "tool is outside run scope")
    remaining = (principal.spend_limit_tokens or 0) - principal.spent_tokens
    if prompt_tokens >= remaining:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "run spend exhausted")
    bounded_output = min(max_tokens or remaining - prompt_tokens, remaining - prompt_tokens)
    if max_tokens is not None and max_tokens > bounded_output:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "request exceeds run spend scope")
    return bounded_output


async def _reserve_run_spend(
    session: AsyncSession,
    principal: Principal,
    usage: UsageTracker,
    *,
    max_tokens: int | None,
) -> None:
    if principal.kind is not PrincipalKind.RUN:
        return
    assert principal.run_id is not None and max_tokens is not None
    from coire_api.run_tokens import InvalidRunToken, charge_run_token

    reservation = usage.prompt_tokens + max_tokens
    try:
        await charge_run_token(session, principal.run_id, reservation)
        await session.commit()
    except InvalidRunToken as exc:
        await usage.finish(UsageOutcome.REFUSED, failure_code="run_spend_exhausted")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "run spend exhausted") from exc
    usage.reserved_tokens = reservation


async def _openai_cold_stream(
    body: ChatCompletionRequest,
    principal: Principal,
    session: AsyncSession,
    settings: Settings,
    usage: UsageTracker,
    request: Request,
    timing: StreamTiming,
) -> AsyncIterator[bytes]:
    task = asyncio.create_task(
        _load_and_resolve(
            body.model,
            principal,
            session,
            settings,
            body.coire_affinity_node,
            **({"variant_id": body.coire_variant_id} if body.coire_variant_id else {}),
        )
    )
    async with cancel_pending_load(task):
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
        usage.bind_resolution(resolved)
        payload = _compatible_payload(body, resolved.model_path)
        await session.rollback()
        rewritten = _rewrite_selector(
            stream(resolved.engine_url, payload, settings, timing),
            body.model,
            private_model_path=resolved.model_path,
        )
        tracked = _tracked_stream(rewritten, usage, request, timing)
        async for chunk in tracked:
            yield chunk
    except (ModelLoadError, TimeoutError) as exc:
        await usage.finish(UsageOutcome.FAILED, failure_code="model_load_failed")
        error = json.dumps({"error": {"message": str(exc), "type": "model_load_error"}})
        yield f"data: {error}\n\n".encode()


async def _anthropic_cold_stream(
    body: AnthropicMessagesRequest,
    principal: Principal,
    session: AsyncSession,
    settings: Settings,
    usage: UsageTracker,
    request: Request,
    timing: StreamTiming,
) -> AsyncIterator[bytes]:
    task = asyncio.create_task(
        _load_and_resolve(
            body.model,
            principal,
            session,
            settings,
            body.coire_affinity_node,
            **({"variant_id": body.coire_variant_id} if body.coire_variant_id else {}),
        )
    )
    async with cancel_pending_load(task):
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
        usage.bind_resolution(resolved)
        payload = to_openai_payload(body, model_path=resolved.model_path)
        await session.rollback()
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
async def list_models(
    principal: CurrentAuthenticated, session: SessionDep, settings: SettingsDep
) -> GatewayModelList:
    rows = (await session.execute(select(ModelRow).order_by(ModelRow.display_name))).scalars().all()
    visible = [
        model
        for model in rows
        if visible_to(is_admin=principal.is_admin, model=model, entitlements=principal.entitlements)
        and (model.source or "studio") == "studio"
        and (principal.kind is not PrincipalKind.RUN or model.id in principal.permitted_model_ids)
    ]
    if principal.kind is PrincipalKind.RUN and principal.permitted_targets:
        visible = []
        for model in rows:
            try:
                await resolve_target(session, model.id, principal)
            except ModelNotFoundError:
                continue
            visible.append(model)
    engines: Sequence[EngineProcessRow] = []
    if visible:
        engines = (
            (
                await session.execute(
                    select(EngineProcessRow).where(
                        EngineProcessRow.model_id.in_([model.id for model in visible]),
                        EngineProcessRow.adapter_id.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
    by_model: dict[object, list[EngineProcessRow]] = {}
    for engine in engines:
        by_model.setdefault(engine.model_id, []).append(engine)
    result = GatewayModelList(
        data=[
            GatewayModel(
                id=model.id,
                created=int(model.created_at.timestamp()),
                coire_load_state=(
                    _load_label(load_state_for(by_model.get(model.id, []))[0])
                    if (model.source or "studio") == "studio"
                    else "loaded"
                ),
                coire_source=ModelSource(model.source or "studio"),
                coire_tags=model.tags,
                coire_description=model.description,
                coire_context_window=model.context_window,
            )
            for model in visible
        ]
    )
    if not any((model.source or "studio") == "studio" for model in rows):
        return result
    adapters = (
        await session.scalars(
            select(TrainingAdapterRow)
            .where(TrainingAdapterRow.state == "ready")
            .order_by(TrainingAdapterRow.selector)
        )
    ).all()
    models_by_id = {model.id: model for model in rows}
    for adapter in adapters:
        try:
            selected = await resolve_target(session, adapter.selector, principal)
        except ModelNotFoundError:
            continue
        model = models_by_id[adapter.model_id]
        pair_engines = (
            await session.scalars(
                select(EngineProcessRow).where(
                    EngineProcessRow.model_id == model.id,
                    EngineProcessRow.variant_id == adapter.base_variant_id,
                    EngineProcessRow.adapter_id == adapter.id,
                )
            )
        ).all()
        result.data.append(
            GatewayModel(
                id=adapter.selector,
                created=int(adapter.created_at.timestamp()),
                coire_load_state=_load_label(load_state_for(list(pair_engines))[0]),
                coire_tags=model.tags,
                coire_description=model.description,
                coire_context_window=model.context_window,
                coire_base_model_id=model.id,
                coire_variant_id=selected.variant.id if selected.variant else None,
                coire_adapter_id=adapter.id,
                coire_verified=adapter.verified,
            )
        )
    return result


@router.post("/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    principal: CurrentAuthenticated,
    session: SessionDep,
    settings: SettingsDep,
) -> object:
    usage = UsageTracker(principal, str(body.model), GatewayProtocol.OPENAI)
    timing = StreamTiming()
    try:
        resolved = await resolve_model(
            session,
            body.model,
            principal,
            body.coire_affinity_node,
            **({"variant_id": body.coire_variant_id} if body.coire_variant_id else {}),
        )
    except ModelNotFoundError as exc:
        await usage.finish(UsageOutcome.REFUSED, failure_code="model_not_found")
        raise HTTPException(status.HTTP_404_NOT_FOUND, "model not found") from exc
    if resolved.source is not ModelSource.STUDIO:
        await usage.finish(UsageOutcome.REFUSED, failure_code="model_not_found")
        raise HTTPException(status.HTTP_404_NOT_FOUND, "model not found")
    usage.bind_resolution(resolved)
    try:
        has_images = any(
            isinstance(message.content, list)
            and any(part.type == "image_url" for part in message.content)
            for message in body.messages
        )
        if has_images and not settings.gateway_inline_visual_enabled:
            raise VisualContextUnavailable("inline image processing is not available yet")
        if has_images and resolved.backend is EngineBackend.MLX_VLM:
            if resolved.visual_capability is None:
                raise VisualContextUnavailable("selected model has no verified visual input")
            body.messages = await normalize_inline_images(
                body.messages, principal, settings, resolved.visual_capability
            )
        usage.prompt_tokens = enforce_context(
            body.messages,
            limit=resolved.context_window,
            output_tokens=body.max_tokens or 0,
            visual=(
                resolved.visual_capability if resolved.backend is EngineBackend.MLX_VLM else None
            ),
        )
    except VisualContextUnavailable as exc:
        await usage.finish(UsageOutcome.REFUSED, failure_code="visual_processing_unavailable")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except ContextLengthError as exc:
        await usage.finish(UsageOutcome.REFUSED, failure_code="context_length")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except TemporaryVisualUnavailable as exc:
        await usage.finish(UsageOutcome.FAILED, failure_code="visual_worker_unavailable")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except TemporaryVisualQuotaExceeded as exc:
        await usage.finish(UsageOutcome.REFUSED, failure_code="visual_temporary_quota")
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    try:
        body.max_tokens = _enforce_run_request_scope(
            principal,
            tools=body.tools,
            prompt_tokens=usage.prompt_tokens,
            max_tokens=body.max_tokens,
        )
    except HTTPException:
        await usage.finish(UsageOutcome.REFUSED, failure_code="run_scope_refused")
        raise
    await _reserve_run_spend(session, principal, usage, max_tokens=body.max_tokens)
    if resolved.engine_url is None or resolved.model_path is None:
        retry_after = await retry_after_seconds(
            session,
            resolved.model_id,
            fallback=settings.gateway_retry_after_s,
            target=resolved.target,
        )
        if body.stream and retry_after >= settings.gateway_wait_ceiling_s:
            await usage.finish(UsageOutcome.REFUSED, failure_code="model_wait_ceiling")
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "estimated model load exceeds wait ceiling",
                headers={"Retry-After": str(retry_after)},
            )
        if not body.coire_wait_for_model:
            await usage.finish(UsageOutcome.REFUSED, failure_code="model_cold")
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "model is not loaded",
                headers={"Retry-After": str(retry_after)},
            )
        if body.stream:
            return _streaming_response(
                _openai_cold_stream(body, principal, session, settings, usage, request, timing),
                usage,
            )
        try:
            resolved = await _load_and_resolve(
                body.model,
                principal,
                session,
                settings,
                body.coire_affinity_node,
                **({"variant_id": body.coire_variant_id} if body.coire_variant_id else {}),
            )
        except (ModelLoadError, TimeoutError) as exc:
            await usage.finish(UsageOutcome.FAILED, failure_code="model_load_failed")
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"model load failed: {exc}",
                headers={"Retry-After": str(retry_after)},
            ) from exc
        if resolved.engine_url is None or resolved.model_path is None:
            await usage.finish(UsageOutcome.FAILED, failure_code="model_not_ready")
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "model load did not become ready"
            )
    usage.bind_resolution(resolved)
    payload = _compatible_payload(body, resolved.model_path)
    await session.rollback()
    try:
        if body.stream:
            rewritten = _rewrite_selector(
                stream(resolved.engine_url, payload, settings, timing),
                body.model,
                private_model_path=resolved.model_path,
            )
            tracked = _tracked_stream(rewritten, usage, request, timing)
            return _streaming_response(tracked, usage)
        result = await complete(resolved.engine_url, payload, settings)
        reported = result.get("usage")
        if isinstance(reported, dict):
            usage.prompt_tokens = int(reported.get("prompt_tokens", usage.prompt_tokens))
            usage.completion_tokens = int(reported.get("completion_tokens", 0))
        result["model"] = str(body.model)
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


@router.post("/completions", response_model=TextCompletionResponse)
async def text_completions(
    body: TextCompletionRequest,
    request: Request,
    principal: CurrentAuthenticated,
    session: SessionDep,
    settings: SettingsDep,
) -> object:
    chat = ChatCompletionRequest(
        **body.model_dump(exclude={"prompt"}),
        messages=[ChatMessage(role="user", content=body.prompt)],
    )
    result = await chat_completions(chat, request, principal, session, settings)
    if isinstance(result, StreamingResponse):
        # Retain UsageStreamingResponse's ASGI disconnect finalizer and headers.
        result.body_iterator = text_stream(cast(AsyncGenerator[bytes], result.body_iterator))
        return result
    if not isinstance(result, dict):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "engine response invalid")
    try:
        return text_response(result)
    except (KeyError, TypeError, ValidationError):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "engine response invalid") from None


@router.post("/messages")
async def anthropic_messages(
    body: AnthropicMessagesRequest,
    request: Request,
    principal: CurrentAuthenticated,
    session: SessionDep,
    settings: SettingsDep,
) -> object:
    usage = UsageTracker(principal, str(body.model), GatewayProtocol.ANTHROPIC)
    timing = StreamTiming()
    try:
        require_anthropic_text(body)
    except ValueError as exc:
        await usage.finish(UsageOutcome.REFUSED, failure_code="unsupported_content")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    try:
        resolved = await resolve_model(
            session,
            body.model,
            principal,
            body.coire_affinity_node,
            **({"variant_id": body.coire_variant_id} if body.coire_variant_id else {}),
        )
    except ModelNotFoundError as exc:
        await usage.finish(UsageOutcome.REFUSED, failure_code="model_not_found")
        raise HTTPException(status.HTTP_404_NOT_FOUND, "model not found") from exc
    if resolved.source is not ModelSource.STUDIO:
        await usage.finish(UsageOutcome.REFUSED, failure_code="model_not_found")
        raise HTTPException(status.HTTP_404_NOT_FOUND, "model not found")
    usage.bind_resolution(resolved)
    try:
        usage.prompt_tokens = enforce_anthropic_context(body, limit=resolved.context_window)
    except ContextLengthError as exc:
        await usage.finish(UsageOutcome.REFUSED, failure_code="context_length")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    try:
        bounded_max_tokens = _enforce_run_request_scope(
            principal,
            tools=body.tools,
            prompt_tokens=usage.prompt_tokens,
            max_tokens=body.max_tokens,
        )
    except HTTPException:
        await usage.finish(UsageOutcome.REFUSED, failure_code="run_scope_refused")
        raise
    assert bounded_max_tokens is not None
    body.max_tokens = bounded_max_tokens
    await _reserve_run_spend(session, principal, usage, max_tokens=body.max_tokens)
    if resolved.engine_url is None or resolved.model_path is None:
        retry_after = await retry_after_seconds(
            session,
            resolved.model_id,
            fallback=settings.gateway_retry_after_s,
            target=resolved.target,
        )
        if body.stream and retry_after >= settings.gateway_wait_ceiling_s:
            await usage.finish(UsageOutcome.REFUSED, failure_code="model_wait_ceiling")
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "estimated model load exceeds wait ceiling",
                headers={"Retry-After": str(retry_after)},
            )
        if not body.coire_wait_for_model:
            await usage.finish(UsageOutcome.REFUSED, failure_code="model_cold")
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "model is not loaded",
                headers={"Retry-After": str(retry_after)},
            )
        if body.stream:
            return _streaming_response(
                _anthropic_cold_stream(body, principal, session, settings, usage, request, timing),
                usage,
            )
        try:
            resolved = await _load_and_resolve(
                body.model,
                principal,
                session,
                settings,
                body.coire_affinity_node,
                **({"variant_id": body.coire_variant_id} if body.coire_variant_id else {}),
            )
        except (ModelLoadError, TimeoutError) as exc:
            await usage.finish(UsageOutcome.FAILED, failure_code="model_load_failed")
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"model load failed: {exc}",
                headers={"Retry-After": str(retry_after)},
            ) from exc
        if resolved.engine_url is None or resolved.model_path is None:
            await usage.finish(UsageOutcome.FAILED, failure_code="model_not_ready")
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "model load did not become ready"
            )
    usage.bind_resolution(resolved)
    payload = to_openai_payload(body, model_path=resolved.model_path)
    await session.rollback()
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
