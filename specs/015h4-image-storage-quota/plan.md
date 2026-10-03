# Implementation Plan: Atomic image storage holds

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | API database and filesystem accounting only. |
| III | Uses existing typed `ImageQuotaRow` persistence; no new wire shape. |
| IV | Caller supplies a server-resolved owner UUID; no public route in this slice. |
| V | No model acquisition. |
| VI | Admission caller will use fixed-label image quota telemetry. |
| VII | Failing ledger tests precede code; cross-process gate remains open. |

## Approach

Serialize quota-row first creation with a transaction-scoped PostgreSQL advisory lock, then lock global before owner rows. Check both configured caps and the disk floor while those rows remain locked. Expose reserve, settle and release operations for later admission/publication workflows. A caller must commit these changes with its input or job row; this module never commits independently.
