# Implementation Plan: Safe Public Model Rewrite

**Branch**: `feat/014bf-gateway-stream-rewrite` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Buffer one bounded SSE event in the shared gateway execution module, normalize line endings, join data lines and rewrite the top-level `model` only after a complete JSON frame is available. Check every emitted frame against the resolved private path and fail on malformed, oversized or incomplete frames. Place this transform inside usage tracking so failures record a failed outcome; always close the upstream iterator. Keep compatible routes as thin protocol adapters. Add regression tests before accepting the new stream behavior.

## Constitution Check

| Principle | Check |
| --- | --- |
| I/II | Only the existing node-owned bare engine stream is consumed. |
| III | Public `/v1` stays OpenAI-compatible with the registry UUID. |
| IV/V | The private verified model path is never published; caller model strings are not sent to an engine. |
| VI | Existing gateway request/error metrics account for failed transforms. |
| VII | Focused execution and route regressions plus the full Python suite gate the change. |

No dependency or architecture change.
