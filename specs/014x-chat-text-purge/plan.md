# Implementation Plan: Deleted Text Conversation Purge

**Branch**: feat/014x-chat-text-purge | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Add a nullable `purged_at` marker and indexed deletion scan in migration 0016. Each API maintenance pass selects at most 100 expired tombstones, locks each conversation, rechecks state and refuses the text-only path if attachments or active turns remain. Clear the active-turn pointer, delete events before turns and messages, release quota rows, scrub metadata and set the marker in one transaction. Keep the tombstone row for owner deletion idempotency. The later file path must delete and verify bytes before marking attached conversations complete.

## Constitution Check

| Principle | Check |
| --- | --- |
| III | Internal marker extends the ORM and reversible migration; public delete contract is unchanged. |
| IV | Tombstoned access stays denied; content is scrubbed and never logged. |
| VI | Purge span/outcome counter and existing Chat dashboard/failure alert. |
| VII | Unit tests and disposable real PostgreSQL migration/purge proof. |

No constitution exception or dependency.
