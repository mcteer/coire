# Implementation Plan: Audited image preset mutations

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | API database service only. |
| III | Existing strict create/update/preset wire types. |
| IV | Caller must be a live human admin at the future route; mutation audit in transaction. |
| V | Registry UUIDs only; no model acquisition. |
| VI | Content-free audit; route telemetry added with route slice. |
| VII | Service tests before implementation and full gates. |

## Approach

Validate current registry state and measured profile, freeze dependency UUIDs and entitlement names, then insert immutable revision rows. Lock the mutable pointer for optimistic updates/retirement. Commit remains the route's responsibility so audit and mutation are atomic.
