# Feature Specification: Deleted Text Conversation Purge

**Feature Branch**: feat/014x-chat-text-purge  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-X1: A deleted text-only conversation with no active turn is purged after a bounded five-minute grace period, before its 24-hour deadline. Purge removes message, turn, event and quota rows, scrubs title/model selection and records completion while retaining a content-free owner tombstone for idempotent DELETE.
- FR-X2: A conversation with an attachment or active turn is not marked purged by the text-only path. Later file cleanup must verify original/derived byte deletion before setting the same completion marker.
- FR-X3: Purge is bounded and idempotent across API processes. Failure is visible through maintenance telemetry; no content appears in logs.

## Independent acceptance

Unit tests cover safe, attached and active cases; a disposable local PostgreSQL proof verifies actual row deletion and migration upgrade/downgrade/re-upgrade. Full gates pass. Blob purge remains parent T066 and Chat remains default-off.
