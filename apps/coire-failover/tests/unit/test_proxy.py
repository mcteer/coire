"""The default relay reaches the node only when a relay token is configured."""

from __future__ import annotations

from uuid import uuid4

import httpx
from pydantic import SecretStr

from coire_core.models.failover import FailoverRelayRequest
from coire_core.models.gateway import ChatCompletionRequest, ChatMessage
from coire_core.settings import Settings
from coire_failover.proxy import completion_relay, fetch_resident


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        failover_relay_token=SecretStr("relay-token"),
        node_listen_port=9400,
        failover_local_relay_url="http://edge-a:9400",
        failover_peer_relay_url="http://edge-b:9400",
    )


async def test_resident_probe_sends_the_relay_credential() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Coire-Failover-Relay"] == "relay-token"
        assert request.url.path == "/node/failover/resident"
        return httpx.Response(
            200,
            json=[{"engine_id": str(uuid4()), "slug": "tiny-model"}],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        engines = await fetch_resident("http://127.0.0.1:9400", "relay-token", client)
    assert len(engines) == 1
    assert engines[0].slug == "tiny-model"


async def test_completion_relay_posts_to_the_local_node() -> None:
    engine_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Coire-Failover-Relay"] == "relay-token"
        assert request.url.host == "edge-a"
        assert request.url.path == f"/node/failover/engines/{engine_id}/chat/completions"
        return httpx.Response(200, json={"id": "completion", "choices": []})

    body = FailoverRelayRequest(
        engine_id=engine_id,
        model_slug="tiny-model",
        request=ChatCompletionRequest(
            model=uuid4(), messages=[ChatMessage(role="user", content="hi")]
        ),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await completion_relay(_settings(), client)(body, "local")
    assert response.status_code == 200


async def test_a_missing_peer_url_fails_closed() -> None:
    settings = _settings().model_copy(update={"failover_peer_relay_url": ""})
    body = FailoverRelayRequest(
        engine_id=uuid4(),
        model_slug="tiny-model",
        request=ChatCompletionRequest(
            model=uuid4(), messages=[ChatMessage(role="user", content="hi")]
        ),
    )
    async with httpx.AsyncClient() as client:
        response = await completion_relay(settings, client)(body, "peer")
    assert response.status_code == 503


async def test_a_missing_local_url_fails_closed() -> None:
    settings = _settings().model_copy(update={"failover_local_relay_url": ""})
    body = FailoverRelayRequest(
        engine_id=uuid4(),
        model_slug="tiny-model",
        request=ChatCompletionRequest(
            model=uuid4(), messages=[ChatMessage(role="user", content="hi")]
        ),
    )
    async with httpx.AsyncClient() as client:
        response = await completion_relay(settings, client)(body, "local")
    assert response.status_code == 503
