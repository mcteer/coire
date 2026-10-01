# Implementation Plan: Conservative image settings and safe errors

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | Settings add no engine, core model load or container. |
| III | Errors use existing typed ProblemDetails; settings do not alter wire contracts. |
| IV | Default disabled and bounded limits prevent accidental admission. |
| V | No acquisition behavior added. |
| VI | No executable path yet; alerts follow services. |
| VII | Tests precede the new settings and errors. |

## Approach

Add explicit `image_*` settings with restrictive defaults and upper bounds. Keep `image_enabled=false` until the parent safety and publication paths pass. Add `CoireError` subclasses with safe, fixed public messages. Document internal settings names and future compose-facing `COIRE_` names; task T012 wires them to containers.
