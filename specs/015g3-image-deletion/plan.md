# Implementation Plan: Owner output deletion and verified purge

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | API-owned blob maintenance only; no engine work. |
| III | Existing `ImageDeletionReceipt` and generated OpenAPI/TS. |
| IV | Live owner guard and immediate tombstone at commit. |
| V | Blob key is data and cannot name a model. |
| VI | Fixed-label delete/purge metrics and oldest pending gauge. |
| VII | Route and maintenance tests precede code. |

## Approach

Lock the published row for tombstoning and commit before responding. Run a bounded maintenance loop inside the API, the only service with the blob mount. Each pass locks a pending tombstone, removes the safe path under a no-follow directory walk, then updates quota counters and `purged_at` in the same database transaction. Missing files are retry-safe after crashes between unlink and commit. Keep image admission disabled.
