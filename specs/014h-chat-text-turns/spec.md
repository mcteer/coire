# Feature Specification: Persistent Text Chat Turns

**Feature Branch**: `feat/014h-chat-text-turns`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-H1: An owner can send a text-only `chat` turn to an eligible published, ready, entitled model. A selected model may change between turns; each turn and assistant message retain the model ID and display-name snapshot. Code actions and attachments are refused until their dedicated children.
- FR-H2: Admission locks the conversation, checks expected revision and one-active-turn state, reserves deterministic input/assistant IDs and persists the accepted turn/event before SSE bytes. Duplicate request ID with the same body observes the saved turn; a changed body conflicts. A send never triggers model acquisition.
- FR-H3: The prompt contains the full saved text history in position order. Context preflight includes framing and bounded output allowance; it never silently drops earlier turns. Engine invocation uses the existing gateway proxy and bare registry-resolved engine path, without loopback HTTP or holding an open database transaction.
- FR-H4: A cold model emits real loading status and bounded keepalives, then streams automatically when ready. Deltas and a terminal event are persisted before emission. Engine failures and disconnects preserve partial answer and safe terminal state; usage is accounted once per admitted turn.
- FR-H5: Missing, foreign, deleted or ineligible conversation/model inputs have safe uniform errors. `GET /conversations/{id}/turns/{turn_id}` reports saved state for recovery. No token, engine path or raw exception leaks in native events.
- FR-H6: New send/status paths add spans, metrics, content-free logs, dashboard and alert coverage, plus operational recovery steps.

## Independent acceptance

Contract tests cover auth/owner/revision/idempotency, model eligibility, history and model snapshots, cold status, streamed/persisted deltas, terminal/usage, engine failure and disconnect. Existing compatible gateway, admin and auth regressions remain green. Local tiny-model integration is a later final 014 gate.
