# Feature Specification: Fenced Studio image job dispatch

**Feature Branch**: `feat/015e18-image-node-dispatch`
**Parent**: `specs/015-image-generation/` (part of T024/T032)
**Dependency**: draft PR #87

## Goal

Accept a typed image job from the scheduler on the authenticated Studio control listener, journal it before contacting the resident private worker, and make duplicate calls safe across timeouts and restarts.

## Acceptance

1. `PUT /node/images/jobs/{job_id}` requires the existing node bearer, exact job/node/instance binding, a ready live image worker and a supported local txt2img request. It is absent from the data listener.
2. Persist queued and reserving states before the one worker dispatch. A repeated identical request returns the persisted state without another `PUT /job`; changed attempt/fence or body conflicts. A network timeout leaves an uncertain journal state for reconciliation, never a blind resend.
3. Worker control uses the private loopback bearer read from the owner-only launch file. The response must match job/attempt/fence. No prompt or secret enters node logs/errors.
4. Contract tests use an ASGI fake worker; no native engine or Studio is run.
