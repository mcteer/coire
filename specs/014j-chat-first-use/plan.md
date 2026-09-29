# Implementation Plan: First-Use Chat UI

**Branch**: `feat/014j-chat-first-use` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Move the common header/dock into an AppShell. Route Chat and Admin by hash while preserving existing admin tabs. Add small typed picker/composer/message components and a page-level conversation hook. The page obtains eligible models only from the Chat API and sends one explicit turn through the one-shot native stream hook. It appends accepted user/assistant messages and persisted deltas, updates revision after admission, retains a failed draft and leaves active history/reconciliation to the next child. Render Markdown with a restrictive URL policy, no raw HTML and no remote images.

## Constitution Check

| Principle | Check |
| --- | --- |
| I, II, II-a | UI calls the gateway only. |
| III | Chat API module and UI use generated Pydantic-derived shapes. |
| IV | Owner session via same-origin credentials; no client-supplied owner. |
| V | Picker IDs come from server-filtered registry results. |
| VI | Existing server telemetry records native requests. |
| VII | Component and hook tests precede release; default-off flag remains. |

No constitution exception or dependency.
