# Implementation Plan: Read-Only Chat Event Observer

**Branch**: feat/014t-chat-observer | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Expose an owner-checked GET event route. Parse the conversation-scoped SSE cursor after authorization, then query bounded saved event pages. Recheck user/key ownership each poll. If retention creates a cursor gap, project a current conversation snapshot and advance to its event cursor. The observer uses no gateway stream, engine slot or Stop path.

## Constitution Check

| Principle | Check |
| --- | --- |
| I, II | Observer reads only the control-plane database. |
| III | Existing shared ChatEvent and ChatSnapshot contracts; generated OpenAPI. |
| IV | Owner and live credential checks on every polling pass. |
| VI | Observer admission span/counter; existing Chat dashboard and alert. |
| VII | Owner/cursor contract and snapshot-gap tests. |

No constitution exception or dependency.
