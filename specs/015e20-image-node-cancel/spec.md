# Feature Specification: Fenced Studio image job cancellation

**Feature Branch**: `feat/015e20-image-node-cancel`
**Parent**: `specs/015-image-generation/` (part of T024/T032/T041)
**Dependency**: draft PR #89

## Goal

Cancel an exact Studio image attempt without permitting it to publish afterward or releasing uncertain memory reservations.

## Acceptance

1. Authenticated control-only `DELETE /node/images/jobs/{job_id}` requires a matching typed `NodeImageCancelRequest`. A wrong job, attempt or fence conflicts; identical repeated cancellation is safe.
2. Persist `cancelling` before contacting a running worker. Send its private `/cancel` command, briefly observe the exact attempt, and if it does not confirm cancellation, stop the exact PID/create-time process group with bounded TERM/KILL escalation. Mark `cancelled` only after worker confirmation or confirmed process death.
3. If the process cannot be identified or stopped, leave `cancelling` and the reservation in place and return a safe 503. A `succeeded` journal state cannot be cancelled. Queued work that was never dispatched cancels locally.
4. Contract tests use fake worker and process supervisor; no native engine or real Studio is run.
