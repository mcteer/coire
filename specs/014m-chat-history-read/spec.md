# Feature Specification: Private Chat History Reads

**Feature Branch**: feat/014m-chat-history-read  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-M1: An owner can page their non-deleted conversations newest-first with a stable opaque timestamp and ID cursor. Admins still see only their own Chat.
- FR-M2: An owner can read a bounded latest message/turn page with model-name snapshots, partial answer, active turn and event cursor. Older pages use a message-position cursor.
- FR-M3: Missing, foreign and deleted IDs return the same 404. A detail page and its event cursor are read under a consistent conversation lock.
- FR-M4: Invalid pagination inputs fail safely; neither endpoint returns private storage paths or raw exception text.

## Independent acceptance

Contract tests cover owner scoping, stable pagination, partial output, and foreign/deleted uniformity. OpenAPI/TypeScript types and existing regression gates pass.
