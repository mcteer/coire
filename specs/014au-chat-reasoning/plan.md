# Implementation Plan: Separate Native Chat Reasoning

**Branch**: `feat/014au-chat-reasoning` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Carry the registry capability profile into admitted turn execution. A small incremental parser holds only an incomplete delimiter suffix and emits typed answer/reasoning pieces; explicitly provided `reasoning_content` is assigned to the reasoning channel for eligible profiles. Persist each bounded channel delta and event in the existing transaction before SSE emission. Render the already typed reasoning field with a closed native disclosure and escaped text. Leave Chat default-off until parent acceptance.

## Constitution Check

| Principle | Check |
| --- | --- |
| III | Existing core message/delta channel contracts are used; no new wire shape. |
| IV | Owner access and persist-before-emit remain in the native Chat path. |
| V | Capability profile selects parsing without a hard-coded model ID. |
| VI | Existing turn spans and counters apply; no reasoning content in telemetry. |
| VII | Parser/stream/browser tests and full local gates. |

No constitution exception or new dependency.
