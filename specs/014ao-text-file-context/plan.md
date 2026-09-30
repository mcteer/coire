# Implementation Plan: Text File Chat Context

**Branch**: `feat/014ao-text-file-context` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Add a core message selection projection, then a reversible nullable prompt snapshot and JSONB selection column on `chat_messages` in migration 0017. Resolve ready attachment extraction from the API-published manifest under the conversation admission transaction. Verify owner, source digest and result identity, refuse empty scans and images, and run the existing conservative context preflight over the exact prompt. Persist that prompt separately from displayed message text. The browser sends selected ready text files with the turn and displays their attribution.

## Constitution Check

| Principle | Check |
| --- | --- |
| II | Core performs no model or tokenizer work; existing gateway context estimate applies. |
| III | Core schema and migration precede API/browser use; regenerate OpenAPI/TypeScript. |
| IV | Owner and conversation checks plus verified published manifest before context use. |
| V | Existing model eligibility remains the send authority. |
| VI | Existing Chat admission/refusal spans and counters cover this path. |
| VII | Contract, migration, browser and repository gates. |

No constitution exception or dependency.
