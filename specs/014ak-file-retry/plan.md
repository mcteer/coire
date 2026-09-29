# Implementation Plan: Explicit File Retry

**Branch**: `feat/014ak-file-retry` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Use the existing strict `ChatFileProcessRequest` contract for `operation=inspect` with no selected pages. Add owner/revision/Origin protected POST under a specific attachment. Lock owner, conversation and attachment; deduplicate request ID before checking revision; require the latest inspect job to be failed attempt one with output cleanup marker. Create a fresh generated ULID attempt-two job bound to the same original digest/key, rebind the existing reservation, and commit attachment state/revision/event together. The scheduler's normal DBOS scan dispatches it. A later child will add render operations to this endpoint.

## Constitution Check

| Principle | Check |
| --- | --- |
| II | API queues metadata only; worker remains isolated CPU parser. |
| III | Existing strict core request and attachment contracts. |
| IV | Owner, revision, request ID and unchanged generated source; no automatic retry. |
| VI | Scoped retry span, outcome count and content-free IDs. |
| VII | Service/route tests, OpenAPI/web type regeneration and gates. |

No constitution exception or dependency.
