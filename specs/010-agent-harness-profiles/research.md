# Research: Agent Harness and Capability Profiles

## R1 — Harness dependency

**Decision**: Pin `pydantic-ai-slim` with only its OpenAI-compatible provider. Pydantic AI is MIT
licensed and supports Python 3.13; the gateway remains the sole provider endpoint.

**Rationale**: The slim distribution avoids unrelated cloud providers while supplying typed agents,
tools, model retries, and output validation. Source: https://pypi.org/project/pydantic-ai/

## R2 — Strategy boundary

**Decision**: Normalize native, JSON-mode, and delimited-text calls into one internal `ToolCall` and
validate every call/result with strict Pydantic contracts. Reasoning removal occurs before parsing.

## R3 — Context budget

**Decision**: Preserve system/task prefixes, append history immutably, replace only the transmitted
view of old turns with a separately stored rolling summary, and truncate tool output head/tail.

## R4 — Verification

**Decision**: Store every evaluation per variant and derive `verified` only from the newest passing
suite for that exact variant/harness/engine combination. Infrastructure failures never score.

## R5 — Image isolation

**Decision**: Build user and ops images from common harness code, but package the admin client only
into the ops distribution. CI inspects installed modules and image contents.
