# Implementation Plan: Persist measured image capability

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | Registry metadata only; no engine/container change. |
| III | Dedicated storage for existing strict `ImageCapabilityProfile`. |
| IV | DB constraint fails closed for unmeasured ready bases. |
| V | No acquisition path; only future admin validation fills profile. |
| VI | Existing registry audit; no live image path in this slice. |
| VII | Migration tests and full gates. |

## Approach

Add nullable JSONB and a check constraint in migration 0027 after 0026. Reflect it in ModelRow. Guard downgrade against non-null profiles and verify on disposable local PostgreSQL.
