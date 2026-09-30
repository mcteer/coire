# Implementation Plan: Private image gallery metadata

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | API metadata read only; no engine/container change. |
| III | Existing `ImageOutput`/`ImageOutputPage` shared contracts; regenerate OpenAPI/TS. |
| IV | Live principal and owner-scoped published rows; no admin bypass. |
| V | Saved recipe references registry IDs as data and triggers no acquisition. |
| VI | Bounded gallery metrics/span and safe row IDs only. |
| VII | Service and route contract tests before implementation. |

## Approach

Add a dedicated output router and service. Query `(created_at,id)` descending with a bounded opaque cursor containing its owner and position, fetch `limit+1`, and project strict shared models. The cursor survives deletion of its boundary row and cannot cross owners. Keep gallery available when new image admission is disabled so owners can retrieve existing records after a feature pause.
