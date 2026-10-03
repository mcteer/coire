# Implementation Plan: Owner image input deletion

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | API metadata and file maintenance only; no engine work on core. |
| III | Reuse the existing `ImageInput` wire contract and regenerate OpenAPI/types for the new route. |
| IV | Current owner and origin checks; generated names and no-follow file handling. |
| V | Deletion cannot trigger acquisition or use imported model identifiers. |
| VI | Existing delete span and fixed-label cleanup counters; pending-age dashboard/alert includes deletion. |
| VII | Contract and failure-path tests precede implementation; full local gates follow. |

## Approach

Commit an owner-scoped tombstone under an input row lock. Refuse active references. Keep ordinary reads absent after the tombstone and return the existing projection only from the DELETE route. The parser already verifies `state=processing` after its worker call. Maintenance finds deleting rows, locks global and owner quota before the input row, rechecks state, unlinks the exact generated original, decrements either the outstanding hold or settled stored usage, and marks purged. Keep generation-source deletion and job cancellation in the parent task until job admission exists.
