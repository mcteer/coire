# Feature Specification: Failed File Output Cleanup

**Feature Branch**: `feat/014aj-failed-file-cleanup`
**Parent acceptance**: [014 Chat Web UI](../014-chat-web-ui/spec.md)

## Requirements

- FR-AJ1: Every terminal failed file job eventually has its generated worker output directory purged, including after native worker restart or ambiguous submit. The visible failure code and first-attempt identity remain intact.
- FR-AJ2: The scheduler uses the authenticated idempotent worker purge route and records an `output_purged` marker only after success. Worker unavailability or active-job refusal retries without a destructive state transition.
- FR-AJ3: A future explicit retry may begin only after the marker exists; failed partial output never shares a quota reservation with a new attempt. Cleanup is bounded, restart-safe and content-free in telemetry.
