"""Fixed-host, text-only frontier provider streams normalized to OpenAI SSE."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from opentelemetry import trace

from coire_api.gateway.resolution import ResolvedModel
from coire_core.models.gateway import ChatMessage
from coire_core.models.registry import ModelSource
from coire_core.settings import Settings

_ENDPOINTS = {
    ModelSource.OPENAI: "https://api.openai.com/v1/chat/completions",
    ModelSource.ANTHROPIC: "https://api.anthropic.com/v1/messages",
}
_MAX_EVENT_BYTES = 64 * 1024
tracer = trace.get_tracer("coire.api.gateway.providers")
logger = logging.getLogger(__name__)


class ProviderUnavailable(Exception):
    """A provider credential or stream is unavailable; never contains its response body."""


@dataclass(frozen=True, slots=True)
class ProviderTarget:
    source: ModelSource
    model_id: uuid.UUID
    provider_model_id: str
    max_output_tokens: int


def target_for(resolved: ResolvedModel) -> ProviderTarget:
    if (
        resolved.source is ModelSource.STUDIO
        or resolved.provider_model_id is None
        or resolved.max_output_tokens is None
    ):
        raise ProviderUnavailable("provider target unavailable")
    return ProviderTarget(
        resolved.source, resolved.model_id, resolved.provider_model_id, resolved.max_output_tokens
    )


def credential_present(source: ModelSource, settings: Settings) -> bool:
    if source is ModelSource.OPENAI:
        return bool(settings.openai_api_key.get_secret_value())
    if source is ModelSource.ANTHROPIC:
        return bool(settings.anthropic_api_key.get_secret_value())
    return False


def _headers(source: ModelSource, settings: Settings) -> dict[str, str]:
    if source is ModelSource.OPENAI:
        key = settings.openai_api_key.get_secret_value()
        headers = {"Authorization": f"Bearer {key}"}
    elif source is ModelSource.ANTHROPIC:
        key = settings.anthropic_api_key.get_secret_value()
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        raise ProviderUnavailable("unsupported provider")
    if not key:
        raise ProviderUnavailable("provider credential unavailable")
    return {**headers, "content-type": "application/json"}


def _messages(messages: list[ChatMessage]) -> tuple[list[dict[str, str]], str | None]:
    system: list[str] = []
    body: list[dict[str, str]] = []
    for message in messages:
        if message.role not in {"system", "user", "assistant"} or not isinstance(
            message.content, str
        ):
            raise ProviderUnavailable("provider supports plain text messages only")
        if message.role == "system":
            system.append(message.content)
        else:
            body.append({"role": message.role, "content": message.content})
    if not body or body[-1]["role"] != "user":
        raise ProviderUnavailable("provider requires a final user message")
    return body, "\n\n".join(system) if system else None


def _payload(
    target: ProviderTarget, messages: list[ChatMessage], output_tokens: int
) -> dict[str, Any]:
    if output_tokens < 1 or output_tokens > target.max_output_tokens:
        raise ProviderUnavailable("provider output limit exceeded")
    body, system = _messages(messages)
    if target.source is ModelSource.OPENAI:
        openai_messages = ([{"role": "system", "content": system}] if system else []) + body
        return {
            "model": target.provider_model_id,
            "messages": openai_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_completion_tokens": output_tokens,
        }
    if target.source is ModelSource.ANTHROPIC:
        result: dict[str, Any] = {
            "model": target.provider_model_id,
            "messages": body,
            "stream": True,
            "max_tokens": output_tokens,
        }
        if system:
            result["system"] = system
        return result
    raise ProviderUnavailable("unsupported provider")


def validate_provider_request(
    target: ProviderTarget, messages: list[ChatMessage], output_tokens: int
) -> None:
    """Run all shape and output checks before reserving any paid allowance."""
    _payload(target, messages, output_tokens)


def _frame(
    target: ProviderTarget, text: str | None = None, *, usage: dict[str, int] | None = None
) -> bytes:
    choice: list[dict[str, Any]] = []
    if text is not None:
        choice = [{"index": 0, "delta": {"content": text}, "finish_reason": None}]
    frame: dict[str, Any] = {
        "id": f"chatcmpl-coire-{target.model_id}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": str(target.model_id),
        "choices": choice,
    }
    if usage is not None:
        frame["usage"] = usage
    return ("data: " + json.dumps(frame, separators=(",", ":")) + "\n\n").encode()


def _parse_event(raw: str) -> dict[str, Any] | None:
    if len(raw.encode()) > _MAX_EVENT_BYTES:
        raise ProviderUnavailable("provider event exceeded limit")
    try:
        event = json.loads(raw)
    except ValueError as exc:
        raise ProviderUnavailable("provider stream malformed") from exc
    if not isinstance(event, dict):
        raise ProviderUnavailable("provider stream malformed")
    return event


@asynccontextmanager
async def _client_scope(client: httpx.AsyncClient | None) -> AsyncIterator[httpx.AsyncClient]:
    if client is not None:
        yield client
    else:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60, connect=10), follow_redirects=False, trust_env=False
        ) as owned:
            yield owned


async def provider_stream(
    target: ProviderTarget,
    messages: list[ChatMessage],
    output_tokens: int,
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[bytes]:
    """Yield one canonical text frame per provider delta and close HTTP on cancellation."""
    payload = _payload(target, messages, output_tokens)
    headers = _headers(target.source, settings)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    generated_bytes = 0
    done = False
    logger.info(
        "provider request started model_id=%s source=%s", target.model_id, target.source.value
    )
    with tracer.start_as_current_span("coire.api.provider.stream") as span:
        span.set_attribute("coire.provider", target.source.value)
        span.set_attribute("coire.model.id", str(target.model_id))
        try:
            async with (
                _client_scope(client) as transport,
                transport.stream(
                    "POST", _ENDPOINTS[target.source], json=payload, headers=headers
                ) as response,
            ):
                if response.status_code != 200:
                    logger.warning(
                        "provider request refused model_id=%s source=%s status=%s",
                        target.model_id,
                        target.source.value,
                        response.status_code,
                    )
                    raise ProviderUnavailable(f"provider HTTP {response.status_code}")
                data_lines: list[str] = []
                async for line in response.aiter_lines():
                    if len(line.encode()) > _MAX_EVENT_BYTES:
                        raise ProviderUnavailable("provider event exceeded limit")
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                        if sum(len(part.encode()) for part in data_lines) > _MAX_EVENT_BYTES:
                            raise ProviderUnavailable("provider event exceeded limit")
                    elif not line and data_lines:
                        raw = "\n".join(data_lines)
                        data_lines.clear()
                        if raw == "[DONE]":
                            done = True
                            break
                        event = _parse_event(raw)
                        assert event is not None
                        if target.source is ModelSource.OPENAI:
                            usage = event.get("usage")
                            if isinstance(usage, dict):
                                prompt_tokens = usage.get("prompt_tokens")
                                completion_tokens = usage.get("completion_tokens")
                            choices = event.get("choices")
                            if isinstance(choices, list) and choices:
                                delta = (
                                    choices[0].get("delta")
                                    if isinstance(choices[0], dict)
                                    else None
                                )
                                content = delta.get("content") if isinstance(delta, dict) else None
                                if isinstance(content, str) and content:
                                    generated_bytes += len(content.encode())
                                    if generated_bytes > 512 * 1024:
                                        raise ProviderUnavailable("provider output exceeded limit")
                                    yield _frame(target, content)
                        else:
                            kind = event.get("type")
                            if kind == "error":
                                raise ProviderUnavailable("provider stream failed")
                            if kind == "message_start":
                                initial = event.get("message")
                                usage = initial.get("usage") if isinstance(initial, dict) else None
                                if isinstance(usage, dict):
                                    prompt_tokens = usage.get("input_tokens")
                            elif kind == "message_delta":
                                usage = event.get("usage")
                                if isinstance(usage, dict):
                                    completion_tokens = usage.get("output_tokens")
                            elif kind == "content_block_delta":
                                delta = event.get("delta")
                                if isinstance(delta, dict) and delta.get("type") == "text_delta":
                                    content = delta.get("text")
                                    if isinstance(content, str) and content:
                                        generated_bytes += len(content.encode())
                                        if generated_bytes > 512 * 1024:
                                            raise ProviderUnavailable(
                                                "provider output exceeded limit"
                                            )
                                        yield _frame(target, content)
                            elif kind == "message_stop":
                                done = True
                                break
        except httpx.HTTPError as exc:
            logger.warning(
                "provider transport failed model_id=%s source=%s error_type=%s",
                target.model_id,
                target.source.value,
                type(exc).__name__,
            )
            raise ProviderUnavailable("provider transport failed") from exc
    if (
        not done
        or not isinstance(prompt_tokens, int)
        or not isinstance(completion_tokens, int)
        or prompt_tokens < 0
        or completion_tokens < 0
    ):
        logger.warning(
            "provider usage missing model_id=%s source=%s completed=%s",
            target.model_id,
            target.source.value,
            done,
        )
        raise ProviderUnavailable("provider stream ended without usage")
    logger.info(
        "provider request completed model_id=%s source=%s prompt_tokens=%s completion_tokens=%s",
        target.model_id,
        target.source.value,
        prompt_tokens,
        completion_tokens,
    )
    yield _frame(
        target,
        usage={
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    )
    yield b"data: [DONE]\n\n"
