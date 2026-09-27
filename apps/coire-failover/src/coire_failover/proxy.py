"""HTTP relay to a node agent. Empty credentials leave the frontend fail-closed."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
from fastapi.responses import JSONResponse, Response, StreamingResponse

from coire_core.models.failover import FailoverRelayRequest, FailoverResidentEngine
from coire_core.settings import Settings

_RELAY_HEADER = "X-Coire-Failover-Relay"
CompletionRelay = Callable[[FailoverRelayRequest, str], Awaitable[Response]]


def relay_token(settings: Settings) -> str:
    return settings.failover_relay_token.get_secret_value().strip()


def _base_url(settings: Settings, destination: str) -> str:
    if destination == "local":
        return settings.failover_local_relay_url.strip()
    if destination == "peer":
        return settings.failover_peer_relay_url.strip()
    return ""


async def fetch_resident(
    base_url: str, token: str, client: httpx.AsyncClient
) -> list[FailoverResidentEngine]:
    """Return the node's resident engines, or nothing when the probe fails."""
    if not base_url or not token:
        return []
    try:
        response = await client.get(
            f"{base_url.rstrip('/')}/node/failover/resident",
            headers={_RELAY_HEADER: token},
            timeout=2,
        )
        response.raise_for_status()
        return [FailoverResidentEngine.model_validate(item) for item in response.json()]
    except (httpx.HTTPError, ValueError):
        return []


def completion_relay(settings: Settings, client: httpx.AsyncClient) -> CompletionRelay:
    """Proxy an authorised completion to the local node or the other Studio."""
    token = relay_token(settings)

    async def relay(body: FailoverRelayRequest, destination: str) -> Response:
        base = _base_url(settings, destination)
        if not token or not base:
            return JSONResponse(
                status_code=503, content={"detail": "not_elected", "reason": "not_elected"}
            )
        url = f"{base.rstrip('/')}/node/failover/engines/{body.engine_id}/chat/completions"
        payload = body.model_dump(mode="json")
        headers = {_RELAY_HEADER: token}
        if not body.request.stream:
            upstream = await client.post(url, json=payload, headers=headers, timeout=300)
            upstream.raise_for_status()
            return JSONResponse(status_code=upstream.status_code, content=upstream.json())

        async def stream() -> AsyncIterator[bytes]:
            async with client.stream(
                "POST", url, json=payload, headers=headers, timeout=httpx.Timeout(300, read=None)
            ) as upstream:
                upstream.raise_for_status()
                async for chunk in upstream.aiter_bytes():
                    yield chunk

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    return relay
