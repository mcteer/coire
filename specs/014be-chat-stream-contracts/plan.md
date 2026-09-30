# Implementation Plan: Chat Stream Contract Audit

**Branch**: `feat/014be-chat-stream-contracts` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Audit the shared SSE parser and native POST/GET hooks against parent T013/T014. Retain the existing typed transport: one explicit POST per send, a cursor-bearing GET observer and no hidden-tab abort for generation. Add the missing test for an expired observer cursor followed by a replacement snapshot. Run web tests, lint and TypeScript build.

## Constitution Check

| Principle | Check |
| --- | --- |
| III | Native event IDs and typed payloads remain the shared wire contract. |
| IV | Observer GET has no generation authority and terminal auth errors stop reconnection. |
| VI | Existing Chat stream/observer telemetry remains in effect. |
| VII | Browser transport, parser, hook and admin regression tests pass. |

No dependency or architecture change.
