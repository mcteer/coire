# Feature Specification: Immediate Chat Conversation Tombstone

**Feature Branch**: feat/014w-chat-tombstone  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-W1: The owner may delete a conversation using its expected revision. The first request atomically tombstones it and returns a purge deadline; a repeat owner request is idempotent. Foreign IDs are 404 and stale live revisions are 409.
- FR-W2: Tombstoned content immediately disappears from list/detail/turn/file access. An active plain-chat turn gets a durable Stop request; the existing observer receives one content-free deletion event and closes.
- FR-W3: The browser requires explicit confirmation, sends the typed revision, removes the selected conversation and its draft, and handles another tab's deletion event.

## Independent acceptance

Contract tests cover owner/idempotency/stale/foreign/Origin/guard and active Stop; an observer test covers deletion event and close; browser tests cover confirmation. Physical content purge remains a later child and the feature stays default-off.
