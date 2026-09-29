# Implementation Plan: Browser Chat History Navigation

**Branch**: feat/014n-chat-history-browser | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Extend the conversation hook with owner-scoped list/detail reads, page cursors, and a monotonically increasing selection generation to discard stale responses. Reuse the typed API functions from 014m. Add a history list component with a narrow-screen disclosure button and visible focus. Restore messages/model attribution from the saved detail. Keep generation controls blocked for recorded active turns until the observer/Stop child can reconcile them. Add component tests and preserve draft behavior.

## Constitution Check

| Principle | Check |
| --- | --- |
| I, II, II-a | Browser reads the gateway only. |
| III | Generated history and message types. |
| IV | Owner scoping remains on the server; no token storage. |
| V | Saved model snapshots are displayed without guessing current eligibility. |
| VI | Existing history read telemetry. |
| VII | Browser interaction/regression tests. |

No constitution exception or dependency.
