# Implementation Plan: Image capacity and lease persistence

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | Schema only; no engine or container. |
| III | Durable state aligns with image contracts and existing UUID registry. |
| IV | Node leases and quota holds are scoped to their owner or job. |
| V | Profile image model refers to the registry. |
| VI | No executable route yet; T013 adds telemetry. |
| VII | Tests precede schema; migration refuses occupied downgrade. |

## Approach

Add quota rows with partial unique indexes, execution leases with exact-subject checks and monotonic fence fields, and measured coexistence profiles. Later service transactions own row locking, quota accounting, admission and release evidence.
