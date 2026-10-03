# Implementation Plan: Persist image asset kind

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | Registry metadata only; no engine/container change. |
| III | Existing `ModelKind` wire enum becomes stored authority. |
| IV | Database consistency and chat refusal for all principals. |
| V | Image assets remain admin acquired; legacy add still refuses unsupported kinds. |
| VI | Existing registry audit and gateway resolution span. |
| VII | Migration/contract tests before code; live disposable Postgres check. |

## Approach

Add one reversible migration after 0025, with default language kind and a backend/source check constraint. Add the field to `ModelRow`, set it on legacy add, and require both kind and backend for chat eligibility. Verify old rows and downgrade guard on disposable local PostgreSQL.
