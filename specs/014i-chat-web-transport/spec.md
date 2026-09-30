# Feature Specification: Browser Chat Event Transport

**Feature Branch**: `feat/014i-chat-web-transport`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-I1: Native turn requests and event envelopes are described by generated OpenAPI types; the browser API module is the only place that calls `fetch` for Chat.
- FR-I2: A POST turn starts at most once per explicit user action. A network loss never repeats the POST. The caller receives typed, ordered events and a terminal outcome.
- FR-I3: SSE parsing accepts fragmented UTF-8, CRLF, multiline data, comments and event IDs. It rejects malformed or mismatched events without showing raw payloads to users.
- FR-I4: The existing admin snapshot stream keeps its cursor/retry behavior. Native generation remains connected when its tab is hidden; cleanup aborts its connection.
- FR-I5: Chat client functions use same-origin credentials, generated request and response shapes, and safe problem details. Cursor state is per stream and can be reset for an unrelated conversation.

## Independent acceptance

Web tests exercise parser boundaries, native POST one-shot behavior, abort and terminal handling, plus existing admin stream regression. Build and lint pass.
