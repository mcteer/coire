# Implementation Plan: Image routing isolation

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | No wrapper, model execution or container changes. |
| III | Existing `ModelKind` and `EngineBackend` contracts. |
| IV | Direct resolution and admin acquisition fail closed; refusal audited. |
| V | Only future audited image acquisition may create image assets. |
| VI | Existing registry audit and resolution span; later image paths use feature telemetry. |
| VII | Routing regression tests before code; full gates. |

## Approach

Use a single backend eligibility predicate for language/vision listings and resolution. Existing failover snapshot already filters `mlx_lm`; retain that gate. Reject non-language `ModelAddRequest.kind` at the legacy admin add boundary with a fixed audit reason.
