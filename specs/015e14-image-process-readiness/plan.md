# Implementation Plan: Image process readiness and re-adoption

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II | Node probes only its Studio child; core never loads a model. |
| II-a | No new container or service. |
| III | Reuse strict load result and process record from coire-core. |
| IV/V | Private token, exact PID/create time/argv, no caller model path. |
| VI | Successful model load is recorded only after authenticated health and durable ready state. |
| VII | Fake process/health tests precede code; real Studio evidence remains open. |

## Approach

Add a bounded private record reader and process-identity helper to the supervisor. On startup, validate the stored config and token files against the record and re-adopt only an exact live bootstrap child. For readiness, call its loopback health with the per-worker token, validate the typed response and immutable identity, then atomically change the recorded state to ready. Keep a full memory hold for an unadopted record.
