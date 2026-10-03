# Implementation Plan: Eligible image preset picker

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | API read only, no engine/container. |
| III | `ImagePresetList` shared contract and generated OpenAPI/TS. |
| IV | Current owner/key/entitlement checks; no admin bypass. |
| V | Registry UUIDs and ready validation only. |
| VI | Bounded image request telemetry and safe logs. |
| VII | Service and route contract tests before code; full gates. |

## Approach

Refactor stored preset resolution into a policy lookup and a live admission wrapper. Listing reads a bounded set of published pointers, reuses the policy lookup for each, and filters against fresh entitlements and key scopes. The feature flag makes the public picker empty until image admission is enabled.
