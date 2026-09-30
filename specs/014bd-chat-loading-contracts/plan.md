# Implementation Plan: Chat Load Contract Regression

**Branch**: `feat/014bd-chat-loading-contracts` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Exercise the authenticated picker through ASGI with mutable engine state. Verify that eviction changes presentation state while model identity/eligibility and the last measured duration persist. Check the starting/unknown path. Keep the existing stream unit tests as evidence for actual queue transitions and safe load failure; this slice adds no production code.

## Constitution Check

| Principle | Check |
| --- | --- |
| I/II/V | No engine or model acquisition starts during tests. |
| III/IV | The owner-scoped picker response stays typed and hides internal node fields. |
| VI | Existing Chat request telemetry covers picker and load outcomes. |
| VII | ASGI contracts and existing stream tests cover the load states. |

No dependency or architecture change.
