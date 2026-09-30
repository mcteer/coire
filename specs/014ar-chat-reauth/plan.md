# Implementation Plan: Chat Reauthentication Prompt

**Branch**: `feat/014ar-chat-reauth` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Classify `ApiError(401)` in the Chat hook and initial App identity read. Render a same-origin sign-in link that reloads through the edge identity layer while keeping sessionStorage untouched. Extend the Chat GET observer with a one-time terminal-status callback so a 401 becomes visible and stops reconnection. Reuse the existing owner-keyed draft loader after sign-in. Test initial, send and observer refusals and draft retention.

## Constitution Check

| Principle | Check |
| --- | --- |
| IV | Authentication remains edge/API enforced; no token storage or bypass. |
| III | Existing API problem details and owner ID contract. |
| VII | Browser tests, lint and build. |

No constitution exception or dependency.
