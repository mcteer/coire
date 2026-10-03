# Feature Specification: Reconcile Studio image job status

**Feature Branch**: `feat/015e19-image-node-status`
**Parent**: `specs/015-image-generation/` (part of T024/T032)
**Dependency**: draft PR #88

## Goal

Let the scheduler observe the exact journaled attempt after a timeout or node restart without sending generation again.

## Acceptance

1. Authenticated control-only `GET /node/images/jobs/{job_id}` requires attempt and fence query values. It returns the validated durable `NodeImageJob` and never starts work.
2. For active work, query only the exact live resident worker over authenticated loopback. Bind its response to the job/attempt/fence and advance the durable state and bounded progress monotonically. Generated outputs move the node status to `transferring` for the later transfer path.
3. An unavailable, dead or mismatched worker response leaves the journal intact and returns a safe 503. Terminal journal states are returned without requiring a live worker.
4. Contract tests prove restart reconciliation, forged response refusal, terminal reads and no second generation command.
