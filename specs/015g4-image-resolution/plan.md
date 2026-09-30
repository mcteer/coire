# Implementation Plan: Image submission settings resolution

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II/II-a | Pure contract and API-side validation; no engine or model load on core. |
| III | Extend the shared `ImageCapabilityProfile`; regenerate OpenAPI and TS. |
| IV | No new route or trust boundary; final authorization remains a job-admission requirement. |
| V | Registry model IDs are checked by the later admission transaction; this helper does not pass caller IDs to engines. |
| VI | No new execution path; admission telemetry follows in the route slice. |
| VII | Failing contract/unit tests precede implementation. |

## Approach

Add optional default values to the profile contract with an all-or-none validator, and require a full set when resolving a job. Overlay an already validated direct or preset request on these values. This pure helper only accepts the basic txt2img shape and calls the existing measured profile validator. Seed resolution occurs once; caller intent hashing happens before this helper in the later admission service.
