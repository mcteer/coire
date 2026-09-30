# Feature Specification: Durable Coding Activity Events

**Feature Branch**: `feat/014bm-durable-run-activity`
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

For a Chat-owned coding run, node activity receipts become owner-scoped Chat events before its container/output cleanup. The API accepts only typed records for the assigned run and turn, persists each sequence once under a locked conversation, and exposes the saved events through existing owner streams and replay. A final status event reports complete, truncated or unavailable without fabricating tool activity. A restarted collector resumes from the durable last sequence. An ordinary MCP run has no Chat conversation and retains its existing result path.

Acceptance: repeated pages and API restarts do not duplicate activity; malformed/foreign receipts fail closed; events persist before the node output is removed; owner observers can replay them; an unavailable spool is reported without inventing tool activity.
