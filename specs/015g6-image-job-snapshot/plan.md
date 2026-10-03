# Implementation Plan: Queued image job settings snapshot

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II | API stores settings only; the Studio supplies runtime facts later. |
| III | Shared Pydantic job and snapshot contracts; OpenAPI freshness checked, with route exposure in the next slice. |
| IV | No route or access change; owner projection follows later. |
| V | IDs remain registry UUIDs; no engine receives request text or model strings here. |
| VI | No new running path; binding telemetry follows scheduler dispatch. |
| VII | Contract and pure binding tests precede implementation. |

## Approach

Add a typed `ImageJobSettingsSnapshot` for the existing JSONB column and an `effective_spec` field to the future public job projection. `resolved` is nullable only before a runtime-bound state. Binding copies the original effective spec into a full resolved record exactly once; any mismatch refuses. No migration is needed because existing JSONB is unpopulated by production image jobs.
