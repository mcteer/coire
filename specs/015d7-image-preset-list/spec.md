# Feature Specification: Eligible image preset picker

**Feature Branch**: `feat/015d7-image-preset-list`
**Parent**: `specs/015-image-generation/` (part of T015, T019, T022)
**Dependency**: draft PR #55

## Goal

Return only currently eligible published image presets to an authenticated human or personal image key. Explicit-capable presets are absent for callers without the live explicit entitlement or key scope. The endpoint does not admit a job.

## Acceptance

1. GET `/api/v1/images/presets` returns `ImagePresetList`, bounded to 100 items and empty while image generation is disabled.
2. Active identity/key and entitlements are rechecked from the database; admin status does not bypass explicit policy.
3. Missing, retired, malformed or non-ready dependency presets are absent without leaking their name or prompt.
4. Hidden required base dependencies contribute their current entitlements; stale principal claims cannot restore them.
