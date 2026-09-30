# Implementation Plan: Image migration verification

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | Local disposable Postgres only; no engine or Studio. |
| III | Check constraints against the schema underlying typed contracts. |
| IV | Exercise owner and idempotency enforcement. |
| V | Existing text/VLM registry rows survive. |
| VI | No executable image path added. |
| VII | Real Postgres migration test, guarded cleanup and existing suite. |

## Approach

Use the existing `COIRE_TEST_POSTGRES_DSN` convention to create a random disposable database. Upgrade from 0022 to head, check old rows and image constraints, verify populated downgrade rejection, remove test records, downgrade and check old rows again. Run on local OrbStack PostgreSQL 17; stop the container afterward.
