# Implementation Plan: User-bound image identity guard

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | API guard only; no model or container. |
| III | Existing shared `ImageForbidden` and principal contracts. |
| IV | Identity, key, Origin and live entitlement rechecks before action. |
| V | No acquisition behavior. |
| VI | Later routes call the image telemetry seam; this slice adds no route. |
| VII | Guard tests precede implementation. |

## Approach

Make a pure preflight guard for principal kind, user binding, scope and browser Origin. Add a live database resolver that rechecks active user, personal key version/revocation/scopes and current entitlement union. Return only the authorized owner UUID. Owner-row filtering, admission audit and revocation-driven cancellation follow in later security slices.
