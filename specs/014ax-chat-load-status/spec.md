# Feature Specification: Observed Chat Load Status

**Feature Branch**: `feat/014ax-chat-load-status`  
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AX1: A cold Chat turn reports actual placement wait as `queued` when the model instance is requested or reserving, and reports loading during launch or warm-up.
- FR-AX2: Queue rank and ETA are absent unless measured data exists. The browser continues to show a plain-language unknown estimate when none is available.
- FR-AX3: Status changes are persisted as owner-only turn events before SSE emission; Stop and access rechecks still apply during the wait.
