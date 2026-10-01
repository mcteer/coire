# Implementation Plan: Image output access policy

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I, II, II-a | API policy only; no engine, model, or container change. |
| III | Existing output and principal contracts; no new wire shape. |
| IV | Owner and live identity/key/entitlement checks; admin has no implicit bypass. |
| V | Output policy is persisted data and cannot cause model acquisition. |
| VI | Reuse the existing content-free route audit and telemetry boundary. |
| VII | Contract tests precede implementation; full gates validate the slice. |

## Approach

Inspect the persisted output tag and resolved recipe mode with strict, bounded field access. Reject corrupt or unknown values. The download helper first checks owner and publication, then rechecks live principal authority with explicit policy derived from both fields. A pure shared-view predicate excludes all but normal, standard output. Routes will call these helpers when output storage and gallery endpoints are built.
