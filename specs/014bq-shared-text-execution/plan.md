# Implementation Plan: Shared Text Execution

**Branch**: `feat/014bq-shared-text-execution` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Move common cold-load/resolution and canonical text payload creation into the gateway execution module. Keep protocol formatting and native Chat persistence at their boundaries. Cover payload serialization, cold-load cancellation and existing OpenAI, Anthropic and native Chat regressions.

## Constitution Check

| Principle | Check |
| --- | --- |
| I/II | Existing Studio engine proxy and placement remain the only execution path. |
| III | Canonical and gateway message types remain in coire-core; no new wire shape. |
| IV/V | Resolve only registry IDs and recheck run credentials after loading. |
| VI | Existing gateway load, first-token, usage and Chat telemetry remain active. |
| VII | Contract, unit and full suites gate this refactor. |

No dependency, migration or architecture deviation.
