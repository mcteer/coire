# Feature Specification: Cold Chat Wait

**Feature Branch**: `feat/014l-chat-cold-wait`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-L1: A cold chat turn emits a persisted loading status with a nullable estimate derived from the latest measured engine load for that model. No measured sample means unknown.
- FR-L2: While the load is ongoing, keepalives preserve the stream; the browser explains warm-up without inventing percentages or queue positions. Generation starts automatically after readiness.
- FR-L3: Load failure emits a safe, actionable terminal message and keeps the user's saved input and any partial output.
- FR-L4: The picker communicates known and unknown estimates before send. Eviction after selection follows the same cold status path.

## Independent acceptance

Backend tests cover known/unknown measurement and load failure; browser tests cover known/unknown picker state and loading/failure display. Existing stream, web and gateway gates pass.
