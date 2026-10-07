"""Legacy text shapes share the authenticated exact-target execution boundary."""

import json
import uuid
from collections.abc import AsyncGenerator

import pytest
from fastapi import FastAPI
from openai.types import Completion
from test_gateway_v1 import app, request  # noqa: F401

from coire_api.gateway.resolution import ModelNotFoundError, ResolvedModel
from coire_api.gateway.text_completions import text_stream


@pytest.mark.parametrize("streaming", [False, True])
async def test_completion_preserves_adapter_selector_and_openai_shape(
    app: FastAPI,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
) -> None:
    model_id, variant_id = uuid.uuid4(), uuid.uuid4()
    selector = f"{model_id}@verified-adapter"
    seen: list[object] = []

    async def resolve(*args: object, **kwargs: object) -> ResolvedModel:
        seen.append((args[1], kwargs))
        return ResolvedModel(
            model_id, "safe", 4096, "/private/model", uuid.uuid4(), "edge", "http://engine"
        )

    async def complete(*args: object) -> dict[str, object]:
        seen.append(args[1])
        return {
            "id": "chatcmpl-text",
            "created": 1,
            "choices": [{"index": 0, "message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }

    async def stream(*args: object) -> AsyncGenerator[bytes]:
        seen.append(args[1])
        frame = json.dumps(
            {
                "id": "chatcmpl-text",
                "created": 1,
                "model": "/private/model",
                "choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            }
        )
        for byte in ("data: " + frame + "\n\ndata: [DONE]\n\n").encode():
            yield bytes([byte])

    monkeypatch.setattr("coire_api.routes.v1.resolve_model", resolve)
    monkeypatch.setattr("coire_api.routes.v1.complete", complete)
    monkeypatch.setattr("coire_api.routes.v1.stream", stream)
    response = await request(
        app,
        "POST",
        "/v1/completions",
        json={
            "model": selector,
            "coire_variant_id": str(variant_id),
            "prompt": "hello",
            "stream": streaming,
        },
    )
    assert response.status_code == 200
    if streaming:
        assert "data: [DONE]" in response.text
        data = json.loads(response.text.split("data: ", 1)[1].split("\n\n", 1)[0])
    else:
        data = response.json()
    result = Completion.model_validate(data)
    assert result.model == selector and result.object == "text_completion"
    assert result.choices[0].text == "hello" and result.usage is not None
    assert result.usage.total_tokens == 4
    assert "/private/model" not in response.text
    assert seen[0] == (selector, {"variant_id": variant_id})
    assert isinstance(seen[1], dict) and seen[1]["messages"] == [
        {"role": "user", "content": "hello"}
    ]


async def test_missing_adapter_completion_never_falls_back(
    app: FastAPI,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def missing(*args: object) -> ResolvedModel:
        raise ModelNotFoundError

    async def forbidden(*args: object) -> None:
        pytest.fail("unresolved target reached engine")

    monkeypatch.setattr("coire_api.routes.v1.resolve_model", missing)
    monkeypatch.setattr("coire_api.routes.v1.complete", forbidden)
    response = await request(
        app, "POST", "/v1/completions", json={"model": f"{uuid.uuid4()}@missing", "prompt": "hello"}
    )
    assert response.status_code == 404


@pytest.mark.parametrize("field", [{"prompt": ["one", "two"]}, {"echo": True}, {"logprobs": 1}])
async def test_unsupported_legacy_options_refuse(
    app: FastAPI,  # noqa: F811
    field: dict[str, object],
) -> None:
    response = await request(
        app,
        "POST",
        "/v1/completions",
        json={"model": str(uuid.uuid4()), "prompt": "hello", **field},
    )
    assert response.status_code == 422


async def test_stream_preserves_control_frames_and_closes_abandoned_source() -> None:
    closed = False

    async def source() -> AsyncGenerator[bytes]:
        nonlocal closed
        try:
            yield b': load keepalive\n\nevent: error\ndata: {"error":{"message":"refused"}}\n\n'
        finally:
            closed = True

    output = text_stream(source())
    assert await anext(output) == b": load keepalive\n\n"
    await output.aclose()
    assert closed
