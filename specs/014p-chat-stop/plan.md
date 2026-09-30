# Implementation Plan: Owner Stop for Plain Chat

**Branch**: feat/014p-chat-stop | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Add a short owner/turn lock transaction that marks active turns stop_requested and writes a typed status event. The original stream polls this durable state through a task and observes it while waiting on an engine chunk or cold load. On Stop it cancels only its own request iterator, closes proxy resources, finalizes usage as stopped and persists a terminal event. The shared load coordinator shields its single-flight task from one request's cancellation. Browser Stop wiring and lease reconciliation follow separate children.

## Constitution Check

| Principle | Check |
| --- | --- |
| I, II, II-a | Stops one gateway request; no engine wrapper or core inference. |
| III | Shared stop request/turn/status and generated OpenAPI. |
| IV | Owner-only state transition; no observer cancellation authority. |
| V | Existing registry model identity and load coordinator. |
| VI | Existing stream/terminal telemetry and structured IDs. |
| VII | Contract and stream cancellation tests. |

No constitution exception or dependency.
