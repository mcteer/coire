"""Model-independent harness orchestration selected entirely from capability data."""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Protocol

from pydantic import BaseModel

from coire_agent.context import SummaryFn, prepare_context
from coire_agent.outputs import RepairFn, reduced_schema, validate_output
from coire_agent.profiles import get_profile
from coire_agent.strategies import ToolParseError, parse_tool_call, strip_reasoning
from coire_agent.telemetry import (
    failures as failure_counter,
)
from coire_agent.telemetry import (
    harness_span,
)
from coire_agent.telemetry import (
    retries as retry_histogram,
)
from coire_agent.telemetry import (
    runs as run_counter,
)
from coire_agent.telemetry import (
    truncations as truncation_counter,
)
from coire_core.models.harness import (
    HarnessMessage,
    HarnessRunRequest,
    HarnessRunResult,
    TaskClass,
    ToolCall,
)


class ModelTransport(Protocol):
    async def complete(
        self, messages: list[HarnessMessage], request: HarnessRunRequest
    ) -> object: ...


ToolExecutor = Callable[[ToolCall], Awaitable[str]]
VerificationFn = Callable[[uuid.UUID], Awaitable[bool]]


class UnverifiedWriteError(PermissionError):
    pass


class Harness:
    def __init__(
        self,
        transport: ModelTransport,
        *,
        allow_ops: bool = False,
        summarize: SummaryFn | None = None,
        repair: RepairFn | None = None,
        verify_variant: VerificationFn | None = None,
        retry_limit: int = 2,
        tool_byte_cap: int = 16_384,
    ) -> None:
        self._transport = transport
        self._allow_ops = allow_ops
        self._summarize = summarize
        self._repair = repair
        self._verify_variant = verify_variant
        self._retry_limit = retry_limit
        self._tool_byte_cap = tool_byte_cap

    async def run_structured(
        self,
        request: HarnessRunRequest,
        output_type: type[BaseModel],
        *,
        reported_prompt_tokens: int = 0,
    ) -> HarnessRunResult:
        if request.task_class is TaskClass.WRITE and (
            self._verify_variant is None or not await self._verify_variant(request.variant_id)
        ):
            raise UnverifiedWriteError(
                f"variant {request.variant_id} is not harness-verified for write tasks"
            )
        profile = get_profile(request.profile, allow_ops=self._allow_ops)
        messages, budget = await prepare_context(
            system_prompt=profile.system_prompt,
            task=request.task,
            history=request.history,
            token_limit=request.context_window,
            reported_prompt_tokens=reported_prompt_tokens,
            summarize=self._summarize,
            tool_byte_cap=self._tool_byte_cap,
        )
        attributes = {"profile": request.profile.value, "strategy": request.tool_strategy.value}
        with harness_span(request.profile.value, request.tool_strategy.value):
            try:
                raw = await self._transport.complete(messages, request)
            except Exception:
                failure_counter.add(1, attributes)
                raise
        if not isinstance(raw, str):
            raise ValueError("structured model response must be text")

        async def retry(error: str) -> str:
            schema = json.dumps(reduced_schema(output_type), separators=(",", ":"))
            feedback = [
                *messages,
                HarnessMessage(
                    role="user",
                    content=f"Validation failed: {error}. Return this reduced schema: {schema}",
                ),
            ]
            retried = await self._transport.complete(feedback, request)
            if not isinstance(retried, str):
                raise ValueError("retry response must be text")
            return retried

        clean, reasoning = strip_reasoning(raw)
        validated, retries = await validate_output(
            clean,
            output_type,
            retry=retry,
            repair=self._repair,
            retry_limit=self._retry_limit,
            strategy=request.output_strategy,
        )
        result = HarnessRunResult(
            run_id=uuid.uuid4(),
            profile=request.profile,
            variant_id=request.variant_id,
            output=validated.model_dump(mode="json"),
            reasoning=reasoning,
            retries=retries,
            context=budget,
        )
        run_counter.add(1, attributes)
        retry_histogram.record(result.retries, attributes)
        truncation_counter.add(result.context.truncation_count, attributes)
        return result

    async def parse_tool(self, request: HarnessRunRequest, payload: object) -> ToolCall:
        error: ToolParseError | None = None
        candidate = payload
        for _ in range(self._retry_limit + 1):
            try:
                return parse_tool_call(request.tool_strategy, candidate)
            except ToolParseError as exc:
                error = exc
                candidate = await self._transport.complete(
                    [HarnessMessage(role="user", content=f"Tool validation failed: {exc}")],
                    request,
                )
        raise ToolParseError(f"tool parsing failed after retry ceiling: {error}")
