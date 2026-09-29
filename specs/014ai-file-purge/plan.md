# Implementation Plan: Deleted Conversation File Purge

**Branch**: `feat/014ai-file-purge` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Add strict `FilePurgeResult` core contract and an authenticated worker DELETE endpoint. The worker rejects active IDs and removes only a generated ULID job directory, succeeding when the directory is already absent. The scheduler scans deleted conversation file jobs in bounded order after their processing deadline and calls the typed private client until an idempotent purge succeeds; then it commits a `purged` marker. API maintenance waits for all related markers, unlinks generated originals, removes quota/job/attachment rows, and lets existing text purge finish. Keep derived volume read-only in the API. Sweep only aged generated upload temp files. Tests cover active/missing/restart/idempotent and filesystem failure boundaries.

## Constitution Check

| Principle | Check |
| --- | --- |
| II | No model/harness on core; isolated worker owns derived writes and deletion. |
| III | Core purge result and strict worker/client wire contract. |
| IV | Dedicated token, generated ULID paths, owner deletion/tombstone precondition. |
| VI | Content-free purge spans, counters and IDs. |
| VII | Worker/scheduler/API tests and full gates. |

No constitution exception or dependency.
