"""OpenAI-compatible `/v1` gateway routes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import CurrentAuthenticated, Principal, PrincipalKind, require_scope
from coire_api.db import EngineProcessRow, ModelRow
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
    load_and_resolve as _load_and_resolve,
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
from coire_api.gateway.loading import ModelLoadError
from coire_api.gateway.provider_budget import ProviderBudgetExceeded, reserve_provider_budget
from coire_api.gateway.providers import (
    ProviderUnavailable,
    credential_present,
    provider_stream,
    target_for,
    validate_provider_request,
)
from coire_api.gateway.proxy import (
    EngineProxyError,
    EngineSaturatedError,
    StreamTiming,
    complete,
    stream,
)
from coire_api.gateway.resolution import (
    ModelNotFoundError,
    resolve_model,
    retry_after_seconds,
)
from coire_api.gateway.temporary import (
    TemporaryVisualQuotaExceeded,
    TemporaryVisualUnavailable,
    normalize_inline_images,
)
from coire_api.gateway.usage import UsageTracker
from coire_api.registry.service import load_state_for, published_ready_entitled, visible_to
from coire_core.models.gateway import (
    AnthropicMessagesRequest,
    ChatCompletionRequest,
    ChatMessage,
    GatewayModel,
    GatewayModelList,
    GatewayProtocol,
    UsageOutcome,
)
from coire_core.models.registry import EngineBackend, LoadState, ModelSource
from coire_core.settings import Settings

router = APIRouter(prefix="/v1", tags=["compatible"], dependencies=[Depends(require_scope("chat"))])


async def _guard_provider_stream(
    source: AsyncIterator[bytes], usage: UsageTracker
) -> AsyncIterator[bytes]:
    try:
        async for frame in source:
            yield frame
    except ProviderUnavailable:
        await usage.finish(UsageOutcome.FAILED, failure_code="provider_stream_failed")
        yield b'data: {"error":{"type":"provider_error","message":"provider request failed"}}\n\n'
        yield b"data: [DONE]\n\n"


async def _guard_anthropic_provider_stream(
    source: AsyncIterator[bytes], usage: UsageTracker
) -> AsyncIterator[bytes]:
    try:
        async for frame in source:
            yield frame
    except ProviderUnavailable:
        await usage.finish(UsageOutcome.FAILED, failure_code="provider_stream_failed")
        yield b'event: error\ndata: {"type":"error","error":{"type":"api_error","message":"provider request failed"}}\n\n'


async def _complete_provider(
    source: AsyncIterator[bytes], usage: UsageTracker
) -> dict[str, object]:
    parts: list[str] = []
    try:
        async for frame in source:
            for line in frame.decode().splitlines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                event = json.loads(line[6:])
                choices = event.get("choices", [])
                if choices:
                    content = choices[0].get("delta", {}).get("content")
                    if isinstance(content, str):
                        parts.append(content)
    except ProviderUnavailable as exc:
        await usage.finish(UsageOutcome.FAILED, failure_code="provider_request_failed")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "provider request failed") from exc
    return {
        "id": f"chatcmpl-coire-{usage.request_id}",
        "object": "chat.completion",
        "created": int(usage.started_at.timestamp()),
        "model": usage.requested_model_id,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "".join(parts)},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.prompt_tokens + usage.completion_tokens,
        },
    }


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
        _load_and_resolve(body.model, principal, session, settings, body.coire_affinity_node)
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
        payload = compatible_text_payload(body, resolved.model_path)
        await session.rollback()
        rewritten = _rewrite_openai_model(
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
        _load_and_resolve(body.model, principal, session, settings, body.coire_affinity_node)
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
        and (
            (model.source or "studio") == "studio"
            or (
                published_ready_entitled(model, principal.entitlements)
                and settings.provider_chat_enabled
                and credential_present(ModelSource(model.source), settings)
            )
        )
        and (principal.kind is not PrincipalKind.RUN or model.id in principal.permitted_model_ids)
    ]
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
        resolved = await resolve_model(session, body.model, principal, body.coire_affinity_node)
    except ModelNotFoundError as exc:
        await usage.finish(UsageOutcome.REFUSED, failure_code="model_not_found")
        raise HTTPException(status.HTTP_404_NOT_FOUND, "model not found") from exc
    usage.bind_resolution(resolved)
    if resolved.source is not ModelSource.STUDIO:
        if any(
            value is not None
            for value in (
                body.tools,
                body.tool_choice,
                body.response_format,
                body.stop,
                body.temperature,
                body.top_p,
                body.coire_affinity_node,
            )
        ):
            await usage.finish(UsageOutcome.REFUSED, failure_code="provider_unsupported_option")
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "provider supports plain text only")
        body.max_tokens = body.max_tokens or min(resolved.max_output_tokens or 0, 1024)
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
    if resolved.source is not ModelSource.STUDIO:
        try:
            target = target_for(resolved)
            if not settings.provider_chat_enabled or not credential_present(
                target.source, settings
            ):
                raise ProviderUnavailable("provider credential unavailable")
            assert body.max_tokens is not None
            validate_provider_request(target, body.messages, body.max_tokens)
            await reserve_provider_budget(
                session, resolved, body.messages, body.max_tokens, usage.request_id
            )
        except ProviderBudgetExceeded as exc:
            await usage.finish(UsageOutcome.REFUSED, failure_code="provider_budget_exhausted")
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
        except ProviderUnavailable as exc:
            await usage.finish(UsageOutcome.REFUSED, failure_code="provider_unavailable")
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        source = _tracked_stream(
            provider_stream(target, body.messages, body.max_tokens, settings),
            usage,
            request,
            timing,
        )
        if body.stream:
            return _streaming_response(_guard_provider_stream(source, usage), usage)
        return await _complete_provider(source, usage)
    if resolved.engine_url is None or resolved.model_path is None:
        retry_after = await retry_after_seconds(
            session, body.model, fallback=settings.gateway_retry_after_s
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
                body.model, principal, session, settings, body.coire_affinity_node
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
    payload = compatible_text_payload(body, resolved.model_path)
    await session.rollback()
    try:
        if body.stream:
            rewritten = _rewrite_openai_model(
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
        resolved = await resolve_model(session, body.model, principal, body.coire_affinity_node)
    except ModelNotFoundError as exc:
        await usage.finish(UsageOutcome.REFUSED, failure_code="model_not_found")
        raise HTTPException(status.HTTP_404_NOT_FOUND, "model not found") from exc
    usage.bind_resolution(resolved)
    if resolved.source is not ModelSource.STUDIO and any(
        value is not None
        for value in (
            body.tools,
            body.tool_choice,
            body.thinking,
            body.output_config,
            body.temperature,
            body.top_p,
            body.top_k,
            body.stop_sequences,
            body.metadata,
            body.service_tier,
            body.context_management,
            body.container,
            body.mcp_servers,
            body.coire_affinity_node,
        )
    ):
        await usage.finish(UsageOutcome.REFUSED, failure_code="provider_unsupported_option")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "provider supports plain text only")
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
    if resolved.source is not ModelSource.STUDIO:
        try:
            target = target_for(resolved)
            if not settings.provider_chat_enabled or not credential_present(
                target.source, settings
            ):
                raise ProviderUnavailable("provider credential unavailable")
            canonical = to_openai_payload(body, model_path=str(body.model))
            raw_messages = canonical["messages"]
            assert isinstance(raw_messages, list)
            messages = [ChatMessage.model_validate(item) for item in raw_messages]
            validate_provider_request(target, messages, body.max_tokens)
            await reserve_provider_budget(
                session, resolved, messages, body.max_tokens, usage.request_id
            )
        except ProviderBudgetExceeded as exc:
            await usage.finish(UsageOutcome.REFUSED, failure_code="provider_budget_exhausted")
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
        except ProviderUnavailable as exc:
            await usage.finish(UsageOutcome.REFUSED, failure_code="provider_unavailable")
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        source = _tracked_stream(
            provider_stream(target, messages, body.max_tokens, settings),
            usage,
            request,
            timing,
        )
        if body.stream:
            adapted = from_openai_stream(source, model=body.model)
            return _streaming_response(_guard_anthropic_provider_stream(adapted, usage), usage)
        result = await _complete_provider(source, usage)
        return from_openai_response(result, model=body.model)
    if resolved.engine_url is None or resolved.model_path is None:
        retry_after = await retry_after_seconds(
            session, body.model, fallback=settings.gateway_retry_after_s
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
                body.model, principal, session, settings, body.coire_affinity_node
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
