# Implementation Plan: Composer Status Acceptance Tests

**Branch**: `feat/014ba-composer-status-tests` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Add component tests that rerender the existing composer across status transitions and assert retained input, disabled Send, restored keyboard submission and Shift+Enter behavior. No runtime implementation or wire contract changes are needed.

## Constitution Check

| Principle | Check |
| --- | --- |
| III | Existing typed status strings remain the wire source. |
| IV | Disabled Send prevents a request during unavailable states. |
| VII | Required browser component acceptance tests, lint and build. |

No constitution exception or dependency.
