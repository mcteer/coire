"""Provider wire translation uses fixed hosts and never passes caller model names."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from pydantic import SecretStr

from coire_api.gateway.providers import ProviderTarget, ProviderUnavailable, provider_stream
from coire_core.models.gateway import ChatMessage
from coire_core.models.registry import ModelSource
from coire_core.settings import Settings

MESSAGES = [ChatMessage(role="user", content="Hello")]


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        openai_api_key=SecretStr("openai-test-secret"),
        anthropic_api_key=SecretStr("anthropic-test-secret"),
    )


def _target(source: ModelSource) -> ProviderTarget:
    return ProviderTarget(source, uuid.uuid4(), "provider-model-1", 128)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [ModelSource.OPENAI, ModelSource.ANTHROPIC])
async def test_provider_stream_normalizes_text_and_usage(source: ModelSource) -> None:
    target = _target(source)
    seen: list[tuple[str, dict[str, object]]] = []
    if source is ModelSource.OPENAI:
        events = [
            {"choices": [{"delta": {"content": "Hi"}}]},
            {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}},
        ]
        wire = (
            "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        )
    else:
        events = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 7}}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}},
            {"type": "message_delta", "usage": {"output_tokens": 2}},
            {"type": "message_stop"},
        ]
        wire = "".join("data: " + json.dumps(event) + "\n\n" for event in events)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), json.loads(request.content)))
        assert request.url.host == (
            "api.openai.com" if source is ModelSource.OPENAI else "api.anthropic.com"
        )
        if source is ModelSource.OPENAI:
            assert request.headers.get("authorization") == "Bearer openai-test-secret"
        else:
            assert request.headers.get("x-api-key") == "anthropic-test-secret"
        return httpx.Response(200, text=wire, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        chunks = [
            chunk
            async for chunk in provider_stream(target, MESSAGES, 16, _settings(), client=client)
        ]
    assert seen[0][1]["model"] == "provider-model-1"
    assert seen[0][1]["stream"] is True
    assert all(b"provider-model-1" not in chunk for chunk in chunks)
    assert b'"content":"Hi"' in chunks[0]
    assert b'"prompt_tokens":7' in chunks[1]
    assert b'"completion_tokens":2' in chunks[1]
    assert chunks[-1] == b"data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_provider_stream_refuses_missing_credential_and_usage() -> None:
    target = _target(ModelSource.OPENAI)
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    with pytest.raises(ProviderUnavailable, match="credential unavailable"):
        async for _ in provider_stream(target, MESSAGES, 16, settings):
            pass

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='data: {"choices": []}\n\ndata: [DONE]\n\n')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderUnavailable, match="without usage"):
            async for _ in provider_stream(target, MESSAGES, 16, _settings(), client=client):
                pass


@pytest.mark.asyncio
async def test_provider_stream_refuses_non_text_before_transport() -> None:
    target = _target(ModelSource.ANTHROPIC)
    with pytest.raises(ProviderUnavailable, match="plain text"):
        async for _ in provider_stream(
            target, [ChatMessage(role="tool", content="private")], 16, _settings()
        ):
            pass
