# Feature Specification: Chat Stream Contract Audit

**Feature Branch**: `feat/014be-chat-stream-contracts`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

Native Chat POST generation is tied to one user action and is never retried by the browser. A separate GET observer follows saved events with a conversation-scoped cursor and may reconnect. On an expired cursor it resumes from zero to receive a replacement snapshot. A hidden tab keeps its in-flight generation connected. Stream parsing preserves fragmented UTF-8, CRLF, comments and multiline events, and auth failures stop retries.

Acceptance: existing transport, API and hook tests pass; a new observer test proves expired-cursor reset, replacement snapshot and GET-only recovery.
