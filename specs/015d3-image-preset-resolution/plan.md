# Implementation Plan: Safe image preset resolution

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | Pure API resolution; no engine, model load or container change. |
| III | Existing strict `ImageSubmitRequest` and `ImagePreset` wire contracts. |
| IV | Derived entitlement union; no request-supplied privilege grant. |
| V | Preset model binding uses registry UUIDs; no acquisition path. |
| VI | Bounded metadata only; later route/service emits telemetry. |
| VII | Focused tests before implementation and full repository gates. |

## Approach

Add a pure resolution module that checks revision/publication, overlays only model-defined user fields, applies the immutable prefix once, preserves explicit policy, and returns requirements for a later live authorization check. Keep DB lookup and admin mutations for subsequent slices.
