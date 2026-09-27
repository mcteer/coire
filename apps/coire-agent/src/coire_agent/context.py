"""Append-only history and bounded transmitted context views."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

from coire_core.models.harness import ContextBudget, HarnessMessage

SummaryFn = Callable[[Sequence[HarnessMessage]], Awaitable[str | None]]


def truncate_tool_output(text: str, byte_cap: int) -> tuple[str, bool]:
    raw = text.encode()
    if len(raw) <= byte_cap:
        return text, False
    marker = b"\n...[truncated]...\n"
    keep = max((byte_cap - len(marker)) // 2, 0)
    bounded = raw[:keep] + marker + raw[-keep:]
    return bounded.decode(errors="replace"), True


async def prepare_context(
    *,
    system_prompt: str,
    task: str,
    history: Sequence[HarnessMessage],
    token_limit: int,
    reported_prompt_tokens: int,
    summarize: SummaryFn | None,
    tool_byte_cap: int,
) -> tuple[list[HarnessMessage], ContextBudget]:
    projected = list(history)
    truncations = 0
    for index, message in enumerate(projected):
        if message.role == "tool":
            content, truncated = truncate_tool_output(message.content, tool_byte_cap)
            if truncated:
                projected[index] = message.model_copy(
                    update={"content": content, "truncated": True}
                )
                truncations += 1

    summarized = 0
    if reported_prompt_tokens >= int(token_limit * 0.8) and len(projected) > 4:
        older, recent = projected[:-4], projected[-4:]
        summary = await summarize(older) if summarize is not None else None
        if summary:
            projected = [HarnessMessage(role="summary", content=summary), *recent]
            summarized = len(older)
        else:
            projected = recent
            summarized = len(older)

    transmitted = [
        HarnessMessage(role="system", content=system_prompt),
        *projected,
        HarnessMessage(role="user", content=task),
    ]
    return transmitted, ContextBudget(
        token_limit=token_limit,
        reported_prompt_tokens=reported_prompt_tokens,
        transmitted_messages=len(transmitted),
        summarized_messages=summarized,
        truncation_count=truncations,
    )
