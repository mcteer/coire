# Implementation Plan: Private Chat File Worker Service

**Branch**: `feat/014aa-file-worker-service` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Mount the dedicated `file_worker_service_token` secret through existing settings. Gate health, process, status and cancel with constant-time bearer comparison. Keep a per-process immutable request/status ledger and one active conversion. Run the CPU parser in one thread so status/cancel remain responsive. Arm a process-exit timer around the native call; scheduler recovery after restart is designed in parent T049. Refused inputs get stable safe codes; unexpected exceptions log only IDs and exception class. Do not expose OpenAPI/docs from the private worker. A separate Compose child enforces exactly one process and private network/volume/resource limits.

## Constitution Check

| Principle | Check |
| --- | --- |
| II/II-a | One CPU service process is planned; parser contains no model, Metal or user harness. |
| III | All request, status, result, cancel and health wire types come from `coire-core`. |
| IV | Every route checks a dedicated secret; jobs are immutable and cancellation suppresses publication. |
| VI | Span, outcome counter and content-free structured logs cover processing. |
| VII | ASGI contract tests exercise auth, job semantics, refusal, cancellation and watchdog arming. |

No constitution exception. Direct `opentelemetry-api==1.44.0` declaration is Apache-2.0, already locked transitively, and required for service-owned spans/counters.
