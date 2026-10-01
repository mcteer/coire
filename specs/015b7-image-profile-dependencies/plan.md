# Implementation Plan: Required image profile dependencies

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | Contract and API lookup only. |
| III | Pydantic first, then generated OpenAPI/TS. |
| IV | Hidden dependency entitlement union cannot be removed by overrides. |
| V | Registry UUIDs only; no caller-supplied engine paths/acquisition. |
| VI | No live route; future admission telemetry remains required. |
| VII | Contract and service tests precede implementation; full gates. |

## Approach

Add an optional bounded tuple to the capability contract. During stored preset lookup, parse the base profile before building the dependency set, add its required IDs, and validate every loaded dependency. Regenerate OpenAPI and the web client types in the same slice.
