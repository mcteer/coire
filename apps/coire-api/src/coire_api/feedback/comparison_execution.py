"""One API-owned regeneration using the existing authenticated bare-engine proxy."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from coire_api.auth import Principal
from coire_api.chat.maintenance import PROCESS_ID
from coire_api.db import ComparisonPairRow, session_scope
from coire_api.feedback.accounting import persist_accounting, settle_comparison, snapshot
from coire_api.feedback.comparisons import erase_pair, locked_pair, pair_event
from coire_api.feedback.eligibility import refresh_owner_principal
from coire_api.feedback.quota import feedback_copy_bytes, require_feedback_capacity
from coire_api.feedback.telemetry import mutations_total, tracer
from coire_api.gateway.context import estimate_chat_tokens
from coire_api.gateway.execution import canonical_text_payload, load_with_ceiling
from coire_api.gateway.proxy import StreamTiming, stream
from coire_api.gateway.resolution import resolve_exact_target
from coire_api.gateway.usage import UsageTracker
from coire_core.errors import FeedbackConflict, FeedbackForbidden, FeedbackValidationError
from coire_core.models.adapters import InferenceTarget
from coire_core.models.feedback import ComparisonAccounting, FeedbackGenerationSettings
from coire_core.models.gateway import ChatMessage, GatewayProtocol, UsageOutcome
from coire_core.settings import Settings

logger = logging.getLogger(__name__)


async def refreshed_principal(principal: Principal) -> Principal:
    async with session_scope() as session:
        return await refresh_owner_principal(session, principal)


async def check_live(pair_id: str, principal: Principal) -> Principal:
    current = await refreshed_principal(principal)
    async with session_scope() as session:
        saved = await session.get(ComparisonPairRow, pair_id)
        if saved is None:
            raise FeedbackForbidden()
        _, pair = await locked_pair(session, current, saved.conversation_id, pair_id, live=True)
        if (
            pair.selection_state != "pending"
            or pair.generation_state != "running"
            or pair.execution.get("owner_process") != PROCESS_ID
        ):
            raise FeedbackConflict("Comparison execution ended")
        if pair.target is None:
            raise FeedbackForbidden("Comparison source was withdrawn")
        await resolve_exact_target(session, InferenceTarget.model_validate(pair.target), current)
        execution = dict(pair.execution)
        execution["lease_expires_at"] = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
        pair.execution = execution
        await session.commit()
    return current


async def while_live[T](operation: Awaitable[T], pair_id: str, principal: Principal) -> T:
    task = asyncio.ensure_future(operation)
    try:
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=0.5)
            if done:
                break
            await check_live(pair_id, principal)
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def run_comparison(pair_id: str, principal: Principal, settings: Settings) -> None:
    usage: UsageTracker | None = None
    source: AsyncIterator[bytes] | None = None
    cid: uuid.UUID | None = None
    outcome = UsageOutcome.FAILED
    with tracer.start_as_current_span(
        "coire.api.feedback.regenerate", record_exception=False, set_status_on_exception=False
    ):
        try:
            current = await refreshed_principal(principal)
            async with session_scope() as session:
                saved = await session.get(ComparisonPairRow, pair_id)
                if saved is None:
                    return
                cid = saved.conversation_id
                conversation, pair = await locked_pair(session, current, cid, pair_id, live=True)
                if pair.generation_state != "queued" or pair.selection_state != "pending":
                    return
                if pair.target is None or pair.prompt is None:
                    raise FeedbackValidationError("Comparison source is unavailable")
                target = InferenceTarget.model_validate(pair.target)
                messages = [ChatMessage.model_validate(value) for value in pair.prompt]
                generation = FeedbackGenerationSettings.model_validate(pair.execution["settings"])
                seed = pair.execution["seed"]
                if type(seed) is not int:
                    raise FeedbackValidationError("Comparison sampling seed is unavailable")
                expected = {
                    key: pair.execution[key]
                    for key in ("tokenizer_sha256", "template_sha256", "runtime_sha256")
                }
                execution = dict(pair.execution)
                request_id = uuid.uuid4()
                started_at = datetime.now(UTC)
                pair.accounting = ComparisonAccounting(
                    request_id=request_id,
                    owner_id=pair.owner_user_id,
                    principal_kind=cast(Literal["user", "admin", "api_key"], current.kind.value),
                    principal_subject=current.subject,
                    api_key_id=current.api_key_id,
                    model_id=target.model_id,
                    variant_id=target.variant_id,
                    adapter_id=target.adapter_id,
                    prompt_tokens=estimate_chat_tokens(messages),
                    started_at=started_at,
                ).model_dump(mode="json")
                execution.update(
                    owner_process=PROCESS_ID,
                    request_id=str(request_id),
                    lease_expires_at=(datetime.now(UTC) + timedelta(seconds=10)).isoformat(),
                )
                pair.execution = execution
                pair.generation_state = "running"
                pair.version += 1
                await pair_event(session, conversation, pair, "comparison.status")
                await session.commit()
            usage = UsageTracker(
                current,
                str(target.model_id),
                GatewayProtocol.OPENAI,
                request_id=request_id,
                started_at=started_at,
            )
            usage.model_id = target.model_id
            usage.variant_id = target.variant_id
            usage.adapter_id = target.adapter_id
            usage.prompt_tokens = estimate_chat_tokens(messages)
            async with session_scope() as session:
                resolved = await resolve_exact_target(session, target, current)
            if resolved.engine_url is None or resolved.model_path is None:
                await while_live(load_with_ceiling(target, settings), pair_id, principal)
                current = await check_live(pair_id, principal)
                async with session_scope() as session:
                    resolved = await resolve_exact_target(session, target, current)
            if (
                resolved.engine_url is None
                or resolved.model_path is None
                or resolved.rendering_identity is None
            ):
                raise FeedbackValidationError("Exact comparison target is unavailable")
            identity = resolved.rendering_identity
            if any(getattr(identity, name) != digest for name, digest in expected.items()):
                raise FeedbackValidationError("Comparison rendering identity changed")
            usage.bind_resolution(resolved)
            payload = canonical_text_payload(
                messages,
                resolved.model_path,
                output_tokens=generation.max_tokens,
                enable_thinking=generation.enable_thinking,
            )
            payload.update(
                temperature=generation.temperature,
                top_p=generation.top_p,
                top_k=generation.top_k,
                min_p=generation.min_p,
                seed=seed,
            )
            source = stream(resolved.engine_url, payload, settings, StreamTiming())
            iterator = source.__aiter__()
            done = False
            while True:
                try:
                    chunk = await while_live(anext(iterator), pair_id, principal)
                except StopAsyncIteration:
                    break
                current = await check_live(pair_id, principal)
                for line in chunk.decode("utf-8", errors="strict").splitlines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        done = True
                        continue
                    if done:
                        raise FeedbackValidationError("Comparison stream contains trailing data")
                    frame = json.loads(data)
                    if not isinstance(frame, dict) or "error" in frame:
                        raise FeedbackValidationError("Comparison engine stream failed")
                    reported = frame.get("usage")
                    if reported is not None:
                        if not isinstance(reported, dict):
                            raise FeedbackValidationError("Comparison usage is invalid")
                        prompt_tokens = reported.get("prompt_tokens", 0)
                        completion_tokens = reported.get("completion_tokens", 0)
                        if (
                            type(prompt_tokens) is not int
                            or type(completion_tokens) is not int
                            or min(prompt_tokens, completion_tokens) < 0
                        ):
                            raise FeedbackValidationError("Comparison usage is invalid")
                        usage.prompt_tokens, usage.completion_tokens = (
                            prompt_tokens,
                            completion_tokens,
                        )
                        usage.reported_token_usage = True
                    choices = frame.get("choices", [])
                    if not isinstance(choices, list) or len(choices) > 1:
                        raise FeedbackValidationError("Comparison choices are invalid")
                    for choice in choices:
                        if not isinstance(choice, dict) or not isinstance(
                            choice.get("delta", {}), dict
                        ):
                            raise FeedbackValidationError("Comparison delta is invalid")
                        delta = choice.get("delta", {})
                        if (
                            delta.get("reasoning_content")
                            or delta.get("reasoning")
                            or delta.get("tool_calls")
                        ):
                            raise FeedbackValidationError("Comparison emitted unsupported content")
                        text = delta.get("content")
                        if text is None:
                            continue
                        if not isinstance(text, str):
                            raise FeedbackValidationError("Comparison text is invalid")
                        if not text:
                            continue
                        if not usage.reported_token_usage:
                            usage.completion_tokens += 1
                        if usage.first_token_at is None:
                            usage.first_token_at = datetime.now(UTC)
                            usage.first_token_duration_ms = max(
                                0.0,
                                (usage.first_token_at - usage.started_at).total_seconds() * 1000,
                            )
                        async with session_scope() as session:
                            conversation, pair = await locked_pair(
                                session, current, cid, pair_id, live=True
                            )
                            if (
                                pair.generation_state != "running"
                                or pair.selection_state != "pending"
                            ):
                                raise FeedbackConflict("Comparison execution ended")
                            candidate = (pair.candidate or "") + text
                            if pair.prompt is None or pair.original is None:
                                raise FeedbackForbidden("Comparison source was withdrawn")
                            counted = feedback_copy_bytes(
                                pair.prompt, pair.original, candidate, allow_identical=True
                            )
                            await require_feedback_capacity(
                                session, max(0, counted - pair.counted_bytes), settings
                            )
                            offset = len(pair.candidate or "")
                            pair.candidate = candidate
                            pair.accounting = snapshot(usage)
                            pair.counted_bytes = counted
                            pair.version += 1
                            await pair_event(
                                session,
                                conversation,
                                pair,
                                "comparison.delta",
                                offset=offset,
                                length=len(text),
                            )
                            await session.commit()
            if not done:
                raise FeedbackValidationError("Comparison stream ended before completion")
            current = await check_live(pair_id, principal)
            async with session_scope() as session:
                conversation, pair = await locked_pair(session, current, cid, pair_id, live=True)
                if pair.generation_state != "running" or pair.selection_state != "pending":
                    raise FeedbackConflict("Comparison execution ended")
                candidate, original = pair.candidate or "", pair.original or ""
                if not candidate:
                    raise FeedbackValidationError("Comparison produced no answer")
                identical = (
                    hashlib.sha256(candidate.encode()).digest()
                    == hashlib.sha256(original.encode()).digest()
                )
                if not identical:
                    if pair.prompt is None:
                        raise FeedbackForbidden("Comparison source was withdrawn")
                    feedback_copy_bytes(pair.prompt, original, candidate)
                pair.generation_state = "identical" if identical else "ready"
                accounting = snapshot(usage)
                accounting["outcome"] = UsageOutcome.SUCCEEDED.value
                pair.accounting = accounting
                pair.version += 1
                if identical:
                    pair.selection_state = "dismissed"
                    await erase_pair(session, pair)
                else:
                    execution = dict(pair.execution)
                    execution.pop("owner_process", None)
                    execution.pop("lease_expires_at", None)
                    execution["usage"] = {
                        "prompt_tokens": usage.prompt_tokens,
                        "completion_tokens": usage.completion_tokens,
                    }
                    pair.execution = execution
                await pair_event(
                    session,
                    conversation,
                    pair,
                    "comparison.terminal" if identical else "comparison.ready",
                )
                await session.commit()
            outcome = UsageOutcome.SUCCEEDED
            mutations_total.add(
                1, {"operation": "regenerate", "outcome": "identical" if identical else "ready"}
            )
        except (Exception, asyncio.CancelledError) as error:
            logger.info(
                "comparison execution ended",
                extra={
                    "comparison_id": pair_id,
                    "user_id": str(principal.user_id),
                    "safe_reason": type(error).__name__,
                },
            )
            if cid is not None:
                try:
                    async with session_scope() as session:
                        conversation, pair = await locked_pair(
                            session, principal, cid, pair_id, live=False
                        )
                        if (
                            pair.generation_state in {"queued", "running"}
                            and pair.selection_state == "pending"
                        ):
                            pair.generation_state = (
                                "cancelled"
                                if isinstance(error, asyncio.CancelledError)
                                else "failed"
                            )
                            pair.selection_state = "dismissed"
                            pair.version += 1
                            await erase_pair(session, pair)
                            await pair_event(session, conversation, pair, "comparison.terminal")
                            await session.commit()
                except Exception as cleanup_error:
                    logger.info(
                        "comparison cleanup deferred",
                        extra={
                            "comparison_id": pair_id,
                            "safe_reason": type(cleanup_error).__name__,
                        },
                    )
        finally:
            if source is not None and hasattr(source, "aclose"):
                try:
                    await source.aclose()
                except Exception as close_error:
                    logger.info(
                        "comparison proxy close failed",
                        extra={"comparison_id": pair_id, "safe_reason": type(close_error).__name__},
                    )
            if usage is not None:
                await persist_accounting(pair_id, usage, outcome)
                await usage.finish(
                    outcome,
                    failure_code=None if outcome is UsageOutcome.SUCCEEDED else "comparison_ended",
                )
                await settle_comparison(pair_id)
