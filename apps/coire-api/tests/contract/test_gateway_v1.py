from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from coire_api.app import create_app
from coire_api.auth import ADMIN, ANONYMOUS, require_principal
from coire_api.db import get_session
from coire_api.gateway.proxy import EngineProxyError
from coire_api.gateway.resolution import ModelNotFoundError, ResolvedModel
from coire_api.gateway.usage import UsageTracker
from coire_api.routes.v1 import _tracked_stream
from coire_core.models.gateway import GatewayProtocol, UsageOutcome
from coire_core.settings import Settings, get_settings

ADMIN_TOKEN = "gateway-contract-admin"


@pytest.fixture
def app(gateway_fake_session: object, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    settings.admin_token = SecretStr(ADMIN_TOKEN)
    settings.identity_legacy_admin_enabled = True
    application = create_app(settings)

    async def session() -> AsyncIterator[object]:
        yield gateway_fake_session

    application.dependency_overrides[get_session] = session
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[require_principal] = lambda: ADMIN

    async def discard_usage(**_: object) -> None:
        return None

    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", discard_usage)
    return application


async def request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    json: object | None = None,
    authenticated: bool = True,
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"} if authenticated else {}
        return await client.request(method, path, json=json, headers=headers)


async def test_models_has_openai_list_shape(app: FastAPI) -> None:
    response = await request(app, "GET", "/v1/models")
    assert response.status_code == 200
    assert response.json() == {"object": "list", "data": []}


async def test_every_compatible_route_requires_authentication(app: FastAPI) -> None:
    app.dependency_overrides[require_principal] = lambda: ANONYMOUS
    for method, path, body in (
        ("GET", "/v1/models", None),
        ("POST", "/v1/chat/completions", {}),
        ("POST", "/v1/messages", {}),
    ):
        response = await request(app, method, path, json=body, authenticated=False)
        assert response.status_code == 401, path
        assert response.headers["www-authenticate"] == "Bearer"


async def test_openai_nonstream_replaces_model_with_resolved_path(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_id = uuid.uuid4()
    seen: dict[str, object] = {}

    async def resolve(*_: object) -> ResolvedModel:
        return ResolvedModel(
            model_id,
            "safe",
            4096,
            "/opt/coire/models/safe",
            uuid.uuid4(),
            "coire-edge-a",
            "http://engine",
        )

    async def complete(_: str, payload: dict[str, object], __: Settings) -> dict[str, object]:
        seen.update(payload)
        return {
            "id": "chatcmpl_1",
            "object": "chat.completion",
            "model": "/opt/coire/models/safe",
            "choices": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }

    monkeypatch.setattr("coire_api.routes.v1.resolve_model", resolve)
    monkeypatch.setattr("coire_api.routes.v1.complete", complete)
    response = await request(
        app,
        "POST",
        "/v1/chat/completions",
        json={"model": str(model_id), "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 200
    assert seen["model"] == "/opt/coire/models/safe"
    assert str(model_id) not in str(seen)
    assert response.json()["model"] == str(model_id)
    assert "/opt/coire/models" not in response.text


async def test_unknown_model_is_rfc9457_problem(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def missing(*_: object) -> ResolvedModel:
        raise ModelNotFoundError

    monkeypatch.setattr("coire_api.routes.v1.resolve_model", missing)
    response = await request(
        app,
        "POST",
        "/v1/chat/completions",
        json={"model": str(uuid.uuid4()), "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == 404


async def test_malformed_model_never_reaches_resolution(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def must_not_resolve(*_: object) -> ResolvedModel:
        raise AssertionError("malformed identifiers must stop at validation")

    monkeypatch.setattr("coire_api.routes.v1.resolve_model", must_not_resolve)
    response = await request(
        app,
        "POST",
        "/v1/chat/completions",
        json={"model": "slug@adapter", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 422


async def test_malformed_inference_request_records_refused_usage(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[dict[str, object]] = []

    async def persist(**kwargs: object) -> None:
        recorded.append(kwargs)

    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", persist)
    response = await request(
        app,
        "POST",
        "/v1/chat/completions",
        json={"model": "not-a-registry-uuid", "messages": []},
    )
    assert response.status_code == 422
    assert len(recorded) == 1
    assert recorded[0]["requested_model_id"] == "not-a-registry-uuid"
    assert recorded[0]["outcome"] is UsageOutcome.REFUSED
    assert recorded[0]["failure_code"] == "request_validation"


async def test_anthropic_nonstream_shape(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    model_id = uuid.uuid4()

    async def resolve(*_: object) -> ResolvedModel:
        return ResolvedModel(
            model_id, "safe", 4096, "/resolved", uuid.uuid4(), "edge", "http://engine"
        )

    async def complete(*_: object) -> dict[str, object]:
        return {
            "id": "chatcmpl_1",
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        }

    monkeypatch.setattr("coire_api.routes.v1.resolve_model", resolve)
    monkeypatch.setattr("coire_api.routes.v1.complete", complete)
    response = await request(
        app,
        "POST",
        "/v1/messages",
        json={
            "model": str(model_id),
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "message"
    assert body["content"] == [{"type": "text", "text": "hi"}]


async def test_openai_stream_uses_sse_and_terminates(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_id = uuid.uuid4()

    async def resolve(*_: object) -> ResolvedModel:
        return ResolvedModel(
            model_id, "safe", 4096, "/resolved", uuid.uuid4(), "edge", "http://engine"
        )

    async def stream(*_: object):  # type: ignore[no-untyped-def]
        yield b'data: {"model":"/resolved","choices":[{"delta":{"content":"hi"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    monkeypatch.setattr("coire_api.routes.v1.resolve_model", resolve)
    monkeypatch.setattr("coire_api.routes.v1.stream", stream)
    response = await request(
        app,
        "POST",
        "/v1/chat/completions",
        json={
            "model": str(model_id),
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert f'"model":"{model_id}"' in response.text
    assert "/resolved" not in response.text
    assert response.text.endswith("data: [DONE]\n\n")


async def test_anthropic_stream_has_required_terminal_event(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_id = uuid.uuid4()

    async def resolve(*_: object) -> ResolvedModel:
        return ResolvedModel(
            model_id, "safe", 4096, "/resolved", uuid.uuid4(), "edge", "http://engine"
        )

    async def stream(*_: object):  # type: ignore[no-untyped-def]
        yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    monkeypatch.setattr("coire_api.routes.v1.resolve_model", resolve)
    monkeypatch.setattr("coire_api.routes.v1.stream", stream)
    response = await request(
        app,
        "POST",
        "/v1/messages",
        json={
            "model": str(model_id),
            "max_tokens": 10,
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert response.status_code == 200
    assert "event: message_start" in response.text
    assert response.text.endswith('event: message_stop\ndata: {"type":"message_stop"}\n\n')


async def test_stream_is_rejected_before_commit_when_warmup_exceeds_ceiling(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_id = uuid.uuid4()

    async def cold(*_: object) -> ResolvedModel:
        return ResolvedModel(model_id, "safe", 4096, None, None, None, None)

    async def slow(*_: object, **__: object) -> int:
        return 60

    settings = app.dependency_overrides[get_settings]()
    settings.gateway_wait_ceiling_s = 10
    monkeypatch.setattr("coire_api.routes.v1.resolve_model", cold)
    monkeypatch.setattr("coire_api.routes.v1.retry_after_seconds", slow)
    response = await request(
        app,
        "POST",
        "/v1/chat/completions",
        json={
            "model": str(model_id),
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_engine_failure_and_disconnect_are_terminal_usage_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes: list[UsageOutcome] = []

    async def persist(**kwargs: object) -> None:
        outcomes.append(kwargs["outcome"])  # type: ignore[arg-type]

    monkeypatch.setattr("coire_api.gateway.usage.persist_usage", persist)

    async def failed() -> AsyncIterator[bytes]:
        yield b"data: partial\n\n"
        raise EngineProxyError("gone")

    failure = UsageTracker(ANONYMOUS, str(uuid.uuid4()), GatewayProtocol.OPENAI)
    assert [chunk async for chunk in _tracked_stream(failed(), failure)] == [b"data: partial\n\n"]

    async def disconnected() -> AsyncIterator[bytes]:
        yield b"data: partial\n\n"
        raise asyncio.CancelledError

    disconnect = UsageTracker(ANONYMOUS, str(uuid.uuid4()), GatewayProtocol.OPENAI)
    with pytest.raises(asyncio.CancelledError):
        async for _ in _tracked_stream(disconnected(), disconnect):
            pass
    assert outcomes == [UsageOutcome.FAILED, UsageOutcome.DISCONNECTED]
