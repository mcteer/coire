"""Shared gateway stream accounting and public-model transport.

Compatible routes and native Chat consume the same proxied engine stream. The proxy owns
engine slots and memory leases; this module owns credential rechecks and once-only usage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from time import monotonic, perf_counter

from fastapi import Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import Receive, Scope, Send

from coire_api.auth import Principal, PrincipalKind
from coire_api.gateway.loading import ModelLoadError, load_model
from coire_api.gateway.proxy import EngineProxyError, StreamTiming
from coire_api.gateway.resolution import ResolvedModel, resolve_model
from coire_api.gateway.telemetry import first_token_duration_ms, overhead_duration_ms
from coire_api.gateway.usage import UsageTracker
from coire_core.models.gateway import ChatCompletionRequest, ChatMessage, UsageOutcome
from coire_core.settings import Settings

logger = logging.getLogger(__name__)


def compatible_text_payload(body: ChatCompletionRequest, model_path: str) -> dict[str, object]:
    """Serialize a compatible text request using only its resolved local model path."""
    payload = body.model_dump(
        mode="json", exclude={"coire_wait_for_model", "coire_affinity_node"}, exclude_none=True
    )
    payload["model"] = model_path
    return payload


def canonical_text_payload(
    messages: list[ChatMessage], model_path: str, *, output_tokens: int
) -> dict[str, object]:
    """Adapt canonical Chat history to the same bare-engine text request shape."""
    return {
        "model": model_path,
        "messages": [message.model_dump(mode="json", exclude_none=True) for message in messages],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": output_tokens,
    }


async def load_and_resolve(
    model_id: uuid.UUID,
    principal: Principal,
    session: AsyncSession,
    settings: Settings,
    affinity_node: str | None = None,
) -> ResolvedModel:
    """Cold-load a registry model and recheck run credentials before engine I/O."""
    # The initial registry lookup may have opened a read transaction. Do not keep it
    # across placement and engine startup, which can take minutes.
    await session.rollback()
    await load_with_ceiling(model_id, settings)
    session.expire_all()
    if principal.run_id is not None:
        from coire_api.run_tokens import run_token_is_active

        if not await run_token_is_active(session, principal.run_id):
            raise ModelLoadError("run credential revoked")
    return await resolve_model(session, model_id, principal, affinity_node)


async def load_with_ceiling(model_id: uuid.UUID, settings: Settings) -> None:
    """Apply the same bounded registry load to compatible and native Chat calls."""
    await asyncio.wait_for(load_model(model_id, settings), timeout=settings.gateway_wait_ceiling_s)


@asynccontextmanager
async def cancel_pending_load(task: asyncio.Task[object]) -> AsyncIterator[None]:
    """Do not leave a cold-load workflow running after its client stream closes."""
    try:
        yield
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def finish_detached(usage: UsageTracker, outcome: UsageOutcome, *, failure_code: str) -> None:
    """Finish usage even if the ASGI caller is already cancelled."""
    task = asyncio.create_task(usage.finish(outcome, failure_code=failure_code))
    with suppress(asyncio.CancelledError):
        await asyncio.shield(task)


class UsageStreamingResponse(StreamingResponse):
    def __init__(self, source: AsyncIterator[bytes], usage: UsageTracker) -> None:
        super().__init__(
            source,
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )
        self._usage = usage

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await finish_detached(
                self._usage,
                UsageOutcome.DISCONNECTED,
                failure_code="client_disconnected",
            )


def streaming_response(source: AsyncIterator[bytes], usage: UsageTracker) -> StreamingResponse:
    """Finalize accounting when the ASGI server closes an abandoned response."""
    return UsageStreamingResponse(source, usage)


def _record_usage_frame(data_lines: list[bytes], usage: UsageTracker) -> None:
    if not data_lines:
        return
    raw = b"\n".join(data_lines)
    if raw == b"[DONE]":
        return
    try:
        event = json.loads(raw)
        if not isinstance(event, dict):
            return
        reported = event.get("usage") or {}
        if isinstance(reported, dict) and reported:
            usage.prompt_tokens = int(reported.get("prompt_tokens", usage.prompt_tokens))
            usage.completion_tokens = int(
                reported.get("completion_tokens", usage.completion_tokens)
            )
        elif event.get("choices", [{}])[0].get("delta", {}).get("content"):
            usage.completion_tokens += 1
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        pass


async def _credential_is_active(principal: Principal) -> bool:
    from coire_api.db import session_scope
    from coire_api.identity.keys import key_is_active
    from coire_api.run_tokens import run_token_is_active

    async with session_scope() as session:
        if principal.kind is PrincipalKind.RUN:
            assert principal.run_id is not None
            return await run_token_is_active(session, principal.run_id)
        return await key_is_active(session, principal)


async def track_stream(
    source: AsyncIterator[bytes],
    usage: UsageTracker,
    request: Request | None = None,
    timing: StreamTiming | None = None,
    stop_signal: asyncio.Event | None = None,
) -> AsyncIterator[bytes]:
    """Forward bytes unchanged while accounting for complete, possibly fragmented SSE frames."""
    first_observed = False
    credential_checked_at = 0.0
    line_buffer = b""
    data_lines: list[bytes] = []
    needs_recheck = request is not None and (
        usage.principal.api_key_id is not None or usage.principal.kind is PrincipalKind.RUN
    )
    # The first recheck can run while the engine is producing its first chunk. It still
    # gates delivery, including a revocation between route authentication and output.
    first_recheck = (
        asyncio.create_task(_credential_is_active(usage.principal)) if needs_recheck else None
    )
    try:
        async for chunk in source:
            if request is not None and await request.is_disconnected():
                await usage.finish(UsageOutcome.DISCONNECTED, failure_code="client_disconnected")
                return
            if needs_recheck:
                assert request is not None
                settings = getattr(request.app.state, "settings", None)
                interval = settings.credential_stream_recheck_s if settings is not None else 1.0
                now = monotonic()
                if first_recheck is not None or now - credential_checked_at >= interval:
                    if first_recheck is not None:
                        active = await first_recheck
                        first_recheck = None
                    else:
                        active = await _credential_is_active(usage.principal)
                    credential_checked_at = now
                    if not active:
                        await usage.finish(UsageOutcome.REFUSED, failure_code="credential_revoked")
                        error = json.dumps(
                            {
                                "error": {
                                    "message": "credential revoked",
                                    "type": "authentication_error",
                                    "code": "credential_revoked",
                                }
                            }
                        )
                        yield f"data: {error}\n\ndata: [DONE]\n\n".encode()
                        return
            if (
                not first_observed
                and timing is not None
                and timing.upstream_started_at is not None
                and timing.first_chunk_at is not None
            ):
                first_observed = True
                first_token_ms = (perf_counter() - timing.request_started_at) * 1000
                engine_ms = (timing.first_chunk_at - timing.upstream_started_at) * 1000
                overhead_ms = max(first_token_ms - engine_ms, 0)
                attributes = {"protocol": usage.protocol.value, "node": usage.node or "none"}
                first_token_duration_ms.record(first_token_ms, attributes)
                overhead_duration_ms.record(overhead_ms, attributes)
                logger.info(
                    "gateway first token request_id=%s model_id=%s engine_id=%s "
                    "first_token_ms=%.2f gateway_overhead_ms=%.2f",
                    usage.request_id,
                    usage.model_id,
                    usage.engine_id,
                    first_token_ms,
                    overhead_ms,
                )
            line_buffer += chunk
            while b"\n" in line_buffer:
                line, line_buffer = line_buffer.split(b"\n", 1)
                line = line.removesuffix(b"\r")
                if not line:
                    _record_usage_frame(data_lines, usage)
                    data_lines.clear()
                elif line.startswith(b"data:"):
                    data_lines.append(line[5:].lstrip(b" "))
            if len(line_buffer) + sum(map(len, data_lines)) > 64 * 1024:
                # The proxy is still authoritative for the wire; only accounting state resets.
                line_buffer = b""
                data_lines.clear()
            yield chunk
    except asyncio.CancelledError:
        stopped = stop_signal is not None and stop_signal.is_set()
        await finish_detached(
            usage,
            UsageOutcome.STOPPED if stopped else UsageOutcome.DISCONNECTED,
            failure_code="user_stop" if stopped else "client_disconnected",
        )
        raise
    except EngineProxyError:
        await usage.finish(UsageOutcome.FAILED, failure_code="engine_stream_failed")
    else:
        await usage.finish(UsageOutcome.SUCCEEDED)
    finally:
        if first_recheck is not None:
            first_recheck.cancel()
            await asyncio.gather(first_recheck, return_exceptions=True)
        # Returning on disconnect from an async-for does not close its source. Close the
        # proxy generator explicitly so its engine slot and memory lease are released now.
        close = getattr(source, "aclose", None)
        if close is not None:
            await close()


async def rewrite_openai_model(
    source: AsyncIterator[bytes], model: uuid.UUID, *, private_model_path: str | None = None
) -> AsyncIterator[bytes]:
    """Rewrite complete SSE frames so split chunks cannot expose a node-local path."""
    public_model = str(model)
    private_path = private_model_path.encode() if private_model_path else None
    buffer = b""
    trailing_cr = b""
    try:
        async for chunk in source:
            data = trailing_cr + chunk
            trailing_cr = b""
            if data.endswith(b"\r"):
                trailing_cr = b"\r"
                data = data[:-1]
            buffer += data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            while b"\n\n" in buffer:
                frame, buffer = buffer.split(b"\n\n", 1)
                if len(frame) > 2 * 1024 * 1024:
                    raise EngineProxyError("engine event exceeds limit")
                lines = frame.split(b"\n")
                data_lines = [
                    line[5:].removeprefix(b" ") for line in lines if line.startswith(b"data:")
                ]
                if not data_lines or b"\n".join(data_lines) == b"[DONE]":
                    rendered = frame + b"\n\n"
                    if private_path and private_path in rendered:
                        raise EngineProxyError("engine event contains private model path")
                    yield rendered
                    continue
                try:
                    event = json.loads(b"\n".join(data_lines))
                except (TypeError, ValueError) as exc:
                    raise EngineProxyError("invalid engine event") from exc
                if not isinstance(event, dict) or "model" not in event:
                    rendered = frame + b"\n\n"
                    if private_path and private_path in rendered:
                        raise EngineProxyError("engine event contains private model path")
                    yield rendered
                    continue
                event["model"] = public_model
                metadata = [line for line in lines if not line.startswith(b"data:")]
                prefix = b"\n".join(metadata)
                if prefix:
                    prefix += b"\n"
                rendered = (
                    prefix + b"data: " + json.dumps(event, separators=(",", ":")).encode() + b"\n\n"
                )
                if private_path and private_path in rendered:
                    raise EngineProxyError("engine event contains private model path")
                yield rendered
            if len(buffer) > 2 * 1024 * 1024:
                raise EngineProxyError("engine event exceeds limit")
        if buffer or trailing_cr:
            raise EngineProxyError("incomplete engine event")
    finally:
        close = getattr(source, "aclose", None)
        if close is not None:
            await close()
