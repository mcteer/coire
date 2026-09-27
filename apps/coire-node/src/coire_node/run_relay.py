"""Fixed-destination gateway relay for one isolated agent-run network."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

ALLOWED_PATHS = frozenset({"chat/completions", "messages"})
REQUEST_HEADERS = frozenset({"authorization", "content-type", "accept"})
RESPONSE_HEADERS = frozenset({"content-type", "retry-after"})


def create_app(
    target: str,
    *,
    max_request_bytes: int,
    client: httpx.AsyncClient | None = None,
) -> FastAPI:
    base = target.rstrip("/")
    owned = client is None
    upstream = client or httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=5.0))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owned:
                await upstream.aclose()

    app = FastAPI(docs_url=None, openapi_url=None, lifespan=lifespan)

    @app.get("/ready")
    async def ready() -> dict[str, bool]:
        return {"ready": True}

    @app.post("/v1/{operation:path}")
    async def proxy(operation: str, request: Request) -> StreamingResponse:
        if operation not in ALLOWED_PATHS:
            raise HTTPException(404, "route not allowed")
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > max_request_bytes:
                    raise HTTPException(413, "request exceeds relay limit")
            except ValueError as exc:
                raise HTTPException(400, "invalid content length") from exc
        body = await request.body()
        if len(body) > max_request_bytes:
            raise HTTPException(413, "request exceeds relay limit")
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.casefold() in REQUEST_HEADERS
        }
        outgoing = upstream.build_request(
            "POST", f"{base}/{operation}", headers=headers, content=body
        )
        response = await upstream.send(outgoing, stream=True)

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()

        safe_headers = {
            key: value
            for key, value in response.headers.items()
            if key.casefold() in RESPONSE_HEADERS
        }
        return StreamingResponse(chunks(), status_code=response.status_code, headers=safe_headers)

    return app


def main() -> None:
    target = os.environ["COIRE_RELAY_TARGET"]
    limit = int(os.environ.get("COIRE_RELAY_MAX_REQUEST_BYTES", str(2 * 1024**2)))
    uvicorn.run(
        create_app(target, max_request_bytes=limit), host="0.0.0.0", port=8080, access_log=False
    )


if __name__ == "__main__":
    main()
