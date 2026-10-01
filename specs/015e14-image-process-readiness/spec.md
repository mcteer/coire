# Feature Specification: Image process readiness and re-adoption

**Feature Branch**: `feat/015e14-image-process-readiness`
**Parent**: `specs/015-image-generation/` (part of T024/T030/T033)
**Dependency**: draft PR #83

## Goal

Prove an image child has loaded its model through authenticated loopback health, and recover exact live identity from node-owned state after an agent restart.

## Acceptance

1. A readiness probe uses the private token file and requires the health response to match instance, PID, create time, port, backend and reservation. A socket response alone never makes the worker ready.
2. Before a probe and after its response, verify that PID, create time and bootstrap command still identify the same live process. Persist `ready` atomically before returning it; invalid or unreachable health keeps `starting` and its reservation.
3. On agent restart, read a bounded owner-only state record and its launch/token files; re-adopt only when they agree and the exact process identity is alive. Unknown, corrupt or dead state is retained for reconciliation, with memory conservatively reserved.
4. Fake-process and ASGI tests cover forged health, changed PID/create time/argv, state tamper and successful recovery. No native engine or real Studio runs.
