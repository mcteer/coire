# Implementation Plan: Human-admin image preset routes

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | API routes only; no engine/container. |
| III | Shared Pydantic request/response and generated OpenAPI/TS. |
| IV | Exact Origin, live human admin role, independent refusal audit and transaction commit. |
| V | No acquisition behavior. |
| VI | Image preset mutation span/metric, structured audit. |
| VII | Route contract tests before implementation and full gates. |

## Approach

Add a dedicated human-admin dependency layered on `CurrentAdmin`, with live role and Origin checks and a separate refusal audit. Routes call the append-only service and commit. Map stale/unique conflicts to `ImageConflict`, then regenerate OpenAPI and TypeScript.
