# Feature Specification: Authenticated Studio Run Activity Reader

**Feature Branch**: `feat/014bl-run-activity-reader`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

The scheduler can read a run's bounded coding activity spool through the node's existing authenticated run API. The reader verifies the requested run owns the container, reads only the fixed per-run output path, validates every `RunActivity` record, and paginates by sequence. A missing spool is explicitly unavailable; an overflow marker is explicitly truncated; malformed, foreign, duplicate, oversized or unsafe archives fail closed.

Acceptance: unauthenticated access is refused; assigned run records return in order with a cursor; other run IDs and malformed archives never return data; unavailable and truncated states are typed.
