"""Node routes for engines."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, HTTPException, Response, status
from fastapi.responses import JSONResponse, StreamingResponse

from coire_core.models.engine import (
    EngineStartRequest,
    EngineState,
    EngineStatus,
    ReconcileRequest,
    ReconcileResult,
)
from coire_core.models.gateway import EngineChatRequest
from coire_node.deps import EngineDep, StoreDep
from coire_node.engines import BudgetExceeded, CopyMissing, NoFreePort

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/node/engines", tags=["engines"])
_proxy_client: httpx.AsyncClient | None = None


def _engine_client() -> httpx.AsyncClient:
    global _proxy_client
    if _proxy_client is None:
        _proxy_client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=8)
        )
    return _proxy_client


async def close_engine_client() -> None:
    global _proxy_client
    client, _proxy_client = _proxy_client, None
    if client is not None:
        await client.aclose()


@router.get("", response_model=list[EngineStatus])
async def list_engines(engines: EngineDep) -> list[EngineStatus]:
    return engines.statuses()


@router.post("", response_model=EngineStatus)
async def start_engine(
    request: EngineStartRequest, response: Response, engines: EngineDep
) -> EngineStatus:
    """Start an engine, or return the one already serving this model (spec FR-019)."""
    try:
        existing, engine_status = engines.start(
            engine_id=request.engine_id,
            slug=request.slug,
            estimate_bytes=request.estimate_bytes,
            chat_template=request.chat_template,
        )
    except CopyMissing as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except BudgetExceeded as exc:
        # The figures travel with the refusal: "no" without them is not actionable.
        raise HTTPException(status.HTTP_409_CONFLICT, exc.refusal.model_dump(mode="json")) from exc
    except NoFreePort as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    response.status_code = status.HTTP_200_OK if existing else status.HTTP_202_ACCEPTED
    return engine_status


@router.post("/reconcile", response_model=ReconcileResult)
async def reconcile(request: ReconcileRequest, engines: EngineDep) -> ReconcileResult:
    return engines.reconcile(request)


@router.get("/{engine_id}", response_model=EngineStatus)
async def get_engine(engine_id: uuid.UUID, engines: EngineDep) -> EngineStatus:
    found = engines.get(engine_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such engine")
    return found


@router.post("/{engine_id}/proxy/v1/chat/completions")
async def proxy_chat_completion(
    engine_id: uuid.UUID, request: EngineChatRequest, engines: EngineDep, store: StoreDep
) -> Response:
    """Carry one authenticated gateway request to a loopback-only bare engine."""
    engine = engines.get(engine_id)
    if engine is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such engine")
    if engine.state is not EngineState.READY:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "engine is not ready")
    expected_model = str(store.path_for(engine.slug or ""))
    if not engine.slug or request.model not in {engine.slug, expected_model}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "model does not match engine")
    url = f"http://127.0.0.1:{engine.port}/v1/chat/completions"
    payload = request.model_dump(mode="json", exclude_none=True)
    # Variant-copy contracts store the validated registry slug, while the bare engine must
    # receive the node-local absolute path. Resolve it only after matching the running
    # engine's immutable slug so no caller-controlled path can cross this boundary.
    payload["model"] = expected_model
    if not request.stream:
        try:
            response = await _engine_client().post(url, json=payload, timeout=300)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "engine request failed") from exc
        return JSONResponse(status_code=response.status_code, content=response.json())

    async def relay() -> AsyncIterator[bytes]:
        timeout = httpx.Timeout(300, read=None)
        async with _engine_client().stream("POST", url, json=payload, timeout=timeout) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                yield chunk

    return StreamingResponse(
        relay(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
    )


@router.delete("/{engine_id}", response_model=EngineStatus, status_code=status.HTTP_202_ACCEPTED)
async def stop_engine(engine_id: uuid.UUID, engines: EngineDep) -> EngineStatus:
    stopped = engines.stop(engine_id)
    if stopped is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such engine")
    return stopped
