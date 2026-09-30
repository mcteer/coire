# Feature Specification: Bounded Coding Run Activity Spool

**Feature Branch**: `feat/014bk-run-activity-spool`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

The Studio coding harness writes content-free, typed started/completed/failed records for actual high-level coding operations into its separate private output mount. Each record has one run ID and increasing sequence. A bounded append-only spool rejects unsafe tool labels and cannot store tool arguments, prompt text, file paths, model content or raw errors.

Acceptance: a coding operation emits paired lifecycle records; an operation failure emits a fixed safe error; bounds and malformed labels are refused without exposing sensitive content. The node reader and Chat event bridge follow in later slices.
