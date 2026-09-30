# Implementation Plan: Private image asset persistence

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | No engine, core model load or container. |
| III | Tables project typed image input/output/grant contracts. |
| IV | Owner FKs and hashed grants keep asset access scoped; routes remain disabled. |
| V | Asset rows reference registry-independent job identity. |
| VI | No executable route yet; T013 adds telemetry with services. |
| VII | Schema safety tests before implementation; guarded migration. |

## Approach

Add input, output, transfer and grant rows. Add composite owner keys to jobs/outputs so FKs prove ownership. Check recipe-only purpose cannot hold normalized generation data. Later service transactions enforce upload limits, purpose use, atomic publication and physical cleanup.
