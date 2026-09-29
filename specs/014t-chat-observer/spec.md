# Feature Specification: Read-Only Chat Event Observer

**Feature Branch**: feat/014t-chat-observer  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-T1: An owner may open a read-only SSE stream of persisted conversation events from a scoped `Last-Event-ID` cursor. Foreign/deleted conversations remain a uniform 404.
- FR-T2: The observer rechecks live user/key access, never starts generation or acquires cancellation authority, and emits only events from its conversation.
- FR-T3: If a requested cursor precedes retained events, the observer sends a replacement snapshot with the current cursor so a tab does not silently miss changes.

## Independent acceptance

Contract tests cover owner/foreign/cursor behavior and OpenAPI SSE shape. A unit test covers cursor-gap replacement. Existing Chat tests and type gates pass.
