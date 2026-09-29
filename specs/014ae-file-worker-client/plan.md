# Implementation Plan: Typed Private File Worker Client

**Branch**: `feat/014ae-file-worker-client` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Add a small `httpx.AsyncClient` wrapper in the API/scheduler package. Load the dedicated worker token from settings, send strict core models, parse strict statuses and verify job identity. Map network, busy, missing and malformed responses to content-free errors without preserving raw HTTP bodies as exception causes. Allow the scheduler to reserve one generated still-image output ID on inspect while worker text/PDF branches ignore it. Retain one-process worker bounds and no caller paths.

## Constitution Check

| Principle | Check |
| --- | --- |
| II | Worker remains CPU-only; client contains no inference or model paths. |
| III | All worker wire shapes use shared strict Pydantic contracts. |
| IV | Dedicated secret, immutable generated ID and content-free exception mapping. |
| VI | Scheduler telemetry is added with durable dispatch in the next child; client errors expose safe categories. |
| VII | Mock HTTP and real parser tests cover request/response compatibility and errors. |

No constitution exception or new dependency; API already uses `httpx`.
