"""Scoped inference relay. Local engines stay on loopback; peers use the relay credential only."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import httpx
from fastapi.responses import JSONResponse, Response, StreamingResponse

from coire_core.models.gateway import EngineChatRequest


def relay_url(base_url: str, engine_id: UUID) -> str:
    """Peer or local node route for one resident engine."""
    return f"{base_url.rstrip('/')}/node/failover/engines/{engine_id}/chat/completions"


async def forward_engine_completion(
    *,
    port: int,
    request: EngineChatRequest,
    client: httpx.AsyncClient,
) -> Response:
    """Proxy one completion to a loopback engine without persisting it."""
    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    forwarded = request.model_dump(mode="json", exclude_none=True)
    if not request.stream:
        upstream = await client.post(url, json=forwarded, timeout=300)
        upstream.raise_for_status()
        return JSONResponse(status_code=upstream.status_code, content=upstream.json())

    async def relay() -> AsyncIterator[bytes]:
        try:
            async with client.stream(
                "POST", url, json=forwarded, timeout=httpx.Timeout(300, read=None)
            ) as upstream:
                upstream.raise_for_status()
                async for chunk in upstream.aiter_bytes():
                    yield chunk
        except httpx.HTTPError:
            yield b'data: {"error":{"message":"engine unavailable","type":"engine_error"}}\n\n'

    return StreamingResponse(
        relay(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
    )


async def forward_peer_completion(
    *,
    base_url: str,
    engine_id: UUID,
    payload: dict[str, object],
    token: str,
    client: httpx.AsyncClient,
    stream: bool,
) -> Response:
    """Forward an already-authorised completion to the other Studio's relay."""
    headers = {"X-Coire-Failover-Relay": token}
    url = relay_url(base_url, engine_id)
    if not stream:
        upstream = await client.post(url, json=payload, headers=headers, timeout=300)
        upstream.raise_for_status()
        return JSONResponse(status_code=upstream.status_code, content=upstream.json())

    async def relay() -> AsyncIterator[bytes]:
        async with client.stream(
            "POST", url, json=payload, headers=headers, timeout=httpx.Timeout(300, read=None)
        ) as upstream:
            upstream.raise_for_status()
            async for chunk in upstream.aiter_bytes():
                yield chunk

    return StreamingResponse(
        relay(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
    )
