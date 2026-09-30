# Implementation Plan: Browser Chat Event Transport

**Branch**: `feat/014i-chat-web-transport` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Expose `ChatEvent` as the SSE response schema in OpenAPI and regenerate TypeScript. Add typed picker/create/status/send functions in the web API module. Extract a strict incremental SSE parser shared by admin snapshot and native turn consumers. Keep admin reconnect semantics. Add a one-shot native turn stream hook with explicit start/abort and terminal delivery; observer GET support follows the history route in a later child.

## Constitution Check

| Principle | Check |
| --- | --- |
| I, II, II-a | Browser consumes gateway events only; no engine or core harness changes. |
| III | Generated `ChatEvent` and request/response types; no handwritten wire contracts. |
| IV | Same-origin credential transport, no token storage, explicit abort. |
| V | Model IDs are picked from registry-backed API results. |
| VI | Existing server telemetry covers requests; no new server execution path. |
| VII | Parser and hook tests plus web build/lint and admin regression. |

No constitution exception or dependency.
