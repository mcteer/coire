# Implementation Plan: Image asset validation and parser contracts

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I | Backend matches bare engine or auxiliary data; no wrapper. |
| II/II-a | Isolated CPU parser commands add no model work to core. |
| III | Every new boundary uses strict shared Pydantic models. |
| IV | Bounded parser operations reject paths and unknown fields. |
| V | Image asset kinds and verified capability remain admin acquisition data. |
| VI | No running path; telemetry follows services. |
| VII | Contract tests first; existing chat/VLM cases stay green. |

## Approach

Add optional image fields with compatible defaults to acquisition projections, then validate kind/backend/profile relationships. Define separate image file process request/result types in `models/files.py`, retaining existing chat operations unchanged. Define a dedicated image activity projection rather than widening existing UUID activity IDs. Regenerate OpenAPI/TypeScript for acquisition schema additions.
