# Implementation Plan: Chat Coding Run Bridge

**Branch**: `feat/014bo-chat-coding-bridge` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Add owner/prior-result source binding and common call/kill operations in `coding_calls.py`. Admit a Chat code action in one database transaction through the existing `runs.create_run` verification gate. Follow persisted events for the controlling POST, stop on disconnect, and publish typed result/terminal events from the scheduler after activity drain and before output removal. Cover cross-owner/source, idempotency, revision, Stop and replay without touching a Studio.

## Constitution Check

| Principle | Check |
| --- | --- |
| I/II | Existing Studio coding harness and bare engine path remain the only execution path. |
| III | `turn.result` uses strict core types; OpenAPI and browser types regenerate. |
| IV/V | Chat owner and prior result bind to workspace, model, run and verified Apply variant; token is revoked before kill. |
| VI | Existing run activity and artifact telemetry applies; Chat accepted/reconcile spans and outcomes are recorded. |
| VII | Source, admission, Stop, result and MCP regressions gate the bridge. |

No new dependency, migration or architecture deviation.
