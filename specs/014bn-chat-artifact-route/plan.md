# Implementation Plan: Chat Apply Artifact Download

**Branch**: `feat/014bn-chat-artifact-route` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Add a shared coding result owner check, expose the Chat artifact route and reuse the established MCP stream with its node status, size and SHA-256 checks. Preserve the existing MCP owner and expiry guard. Test owner, parent, call/run/result/artifact mismatch, tombstone and route security; regenerate OpenAPI and web types.

## Constitution Check

| Principle | Check |
| --- | --- |
| I/II | Bundle stays in the Studio's existing private output; core streams bytes only. |
| III | Route and result use existing strict `coire-core` contracts and generated types. |
| IV | Chat owner, active conversation, Apply call/run and artifact are all bound before download. |
| VI | Existing artifact download span, counter and content-free log cover the stream; Chat adds a scoped span. |
| VII | Contract tests and API image gates cover the new route. |

No dependency, schema migration or architecture deviation.
