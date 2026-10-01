# Implementation Plan: Image job and compatible projections

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | Contracts only; no engine, core model load or container change. |
| III | All wire shapes are strict coire-core Pydantic models. |
| IV | Safe error fields and no implicit authority in grants or imports. |
| V | Registry references stay UUIDs. |
| VI | No executable service path added; telemetry follows routes. |
| VII | Contract tests and full Python gates precede review. |

## Approach

Extend the preceding request/recipe models with bounded native projections and compatible request/response models. Validate event-state pairs and mutually exclusive URL/base64 data. This branch is created from main and carries 015b's commit until that prerequisite merges; review only its incremental diff against `feat/015b-image-contracts`.
