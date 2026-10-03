# Implementation Plan: Live image preset lookup

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | Database lookup only; no engine/container changes. |
| III | Parse persisted defaults through strict image contracts. |
| IV | Live principal/entitlement recheck in same transaction. |
| V | Registry UUIDs and verified ready state; no acquisition. |
| VI | Existing authorization refusal audit at route boundary; no route enabled. |
| VII | Focused tests before implementation and full repository gates. |

## Approach

Load the current preset and revision under row locks, parse their stored data, gather all frozen and effective registry dependency IDs, validate kind/state/backend, then call the pure resolver and live authorization guard before returning. Keep audit invocation at future route/service boundaries.
