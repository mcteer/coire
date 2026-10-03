# Feature Specification: Authenticated resident image worker control

**Feature Branch**: `feat/015e11-image-worker-control`
**Parent**: `specs/015-image-generation/` (part of T024/T029/T030/T032)
**Dependency**: draft PR #80

## Goal

Expose the resident Studio image executor only on a loopback worker control app with a per-worker secret, typed commands and fenced replay/status behavior. Node launch, journal and re-adoption follow separately.

## Acceptance

1. Every worker route requires a strong per-worker bearer secret using constant-time comparison. The app binds to loopback when served; docs and OpenAPI routes are disabled.
2. A typed run returns quickly with a running status and runs in a background thread. Only one generation executes at a time. Identical binding/request replay returns the current status; changed payload or another concurrent job returns 409.
3. Status and cancel require the exact job, attempt and fence. Cancel sets a cooperative flag and reports `cancelled` only after execution stops; coire-node hard termination remains required when callbacks cannot run.
4. Generated status lists bounded output digests/byte counts without paths or prompts. Failures have a stable safe code. Tests use fake pipeline and ASGI transport; no engine or Studio runs.
