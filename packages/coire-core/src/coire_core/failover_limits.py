"""Conservative text-only bounds and bounded local identity counters (no billing state)."""

from __future__ import annotations

import time
import unicodedata
from collections import deque

from coire_core.models.failover import FailoverLimits, FailoverModel
from coire_core.models.gateway import ChatCompletionRequest


class FailoverLimitError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def effective_limits(subject: FailoverLimits, model: FailoverModel) -> FailoverLimits:
    """A signed model policy can tighten every subject limit, never enlarge it."""
    return FailoverLimits(
        prompt_tokens=min(subject.prompt_tokens, model.limits.prompt_tokens),
        output_tokens=min(subject.output_tokens, model.limits.output_tokens),
        requests_per_minute=min(subject.requests_per_minute, model.limits.requests_per_minute),
        concurrency=min(subject.concurrency, model.limits.concurrency),
    )


def bounded_text_request(
    request: ChatCompletionRequest,
    model: FailoverModel,
    limits: FailoverLimits,
) -> ChatCompletionRequest:
    if (
        request.tools
        or request.tool_choice is not None
        or request.response_format is not None
        or request.coire_affinity_node is not None
    ):
        raise FailoverLimitError("unsupported_capability")
    # UTF-8 byte count is deliberately conservative; no tokenizer/weights run on core or frontend.
    # Add a fixed conservative allowance for the approved text chat template on every message.
    prompt_bound = 0
    for message in request.messages:
        if (
            not isinstance(message.content, str)
            or message.role == "tool"
            or message.tool_call_id is not None
            or message.name is not None
            or message.model_extra
        ):
            raise FailoverLimitError("unsupported_capability")
        try:
            byte_bound = max(
                len(message.content.encode("utf-8")),
                len(unicodedata.normalize("NFKD", message.content).encode("utf-8")),
            )
        except UnicodeError as exc:
            raise FailoverLimitError("unsupported_capability") from exc
        prompt_bound += byte_bound + 256
    output_limit = min(limits.output_tokens, model.limits.output_tokens)
    output = request.max_tokens or min(output_limit, model.context_window)
    if (
        prompt_bound > min(limits.prompt_tokens, model.limits.prompt_tokens, model.context_window)
        or output > output_limit
        or prompt_bound + output > model.context_window
    ):
        raise FailoverLimitError("request_limit_exceeded")
    return request.model_copy(update={"max_tokens": output, "coire_wait_for_model": False})


class IdentityCounters:
    def __init__(self) -> None:
        self._history: dict[str, deque[float]] = {}
        self._active: dict[str, int] = {}

    def acquire(self, subject: str, limits: FailoverLimits) -> None:
        now = time.monotonic()
        if subject not in self._history and len(self._history) >= 10000:
            raise FailoverLimitError("request_limit_exceeded")
        history = self._history.setdefault(subject, deque())
        while history and now - history[0] >= 60:
            history.popleft()
        if (
            len(history) >= limits.requests_per_minute
            or self._active.get(subject, 0) >= limits.concurrency
        ):
            raise FailoverLimitError("request_limit_exceeded")
        history.append(now)
        self._active[subject] = self._active.get(subject, 0) + 1

    def release(self, subject: str) -> None:
        self._active[subject] = max(0, self._active.get(subject, 0) - 1)
