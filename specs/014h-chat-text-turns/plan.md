# Implementation Plan: Persistent Text Chat Turns

**Branch**: `feat/014h-chat-text-turns` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Add a short locked admission transaction in the chat service. It checks owner, revision, eligibility, duplicate identity/body, full text context and active-turn constraints, then persists two messages, the turn and an accepted event. Return an SSE response backed by a generator that uses fresh short sessions only for state/event writes. Resolve/load through existing gateway primitives, stream through the existing proxy and shared usage tracker, and save each bounded answer batch before yielding its native event. Finish once with usage and a safe terminal event. A GET status route reads saved turn/message state. Reconciliation, observer replay, explicit Stop and coding are later children; the default-off deployment flag prevents partial release.

The output allowance is the lower of the configured cap and one quarter of the selected model's context window, with a one-token floor. The admission transaction flushes messages, then turn, then accepted event before updating the conversation's active-turn FK; this order was verified against disposable PostgreSQL. Duplicate sends follow saved events read-only and do not own cancellation.

## Constitution Check

| Principle | Check |
| --- | --- |
| I, II, II-a | Only the existing gateway proxies a node-owned bare engine. |
| III | Native requests/events/status use shared Pydantic contracts and generated OpenAPI. |
| IV | Owner lock, verified principal, exact Origin, no path or secret in events. |
| V | Eligible registry ID and resolved local model path only; no acquisition. |
| VI | Send/status spans, metrics, dashboard and alert; content-free logs. |
| VII | Contract tests before code; existing `/v1` and full regression gates. |

No constitution exception, dependency or migration.
