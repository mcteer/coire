# Implementation Plan: Node-owned image process launch

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II | Only coire-node spawns the native child with explicit argv; core loads no model. |
| II-a | Same native Studio installation; no container/service added. |
| III | Strict process record in coire-core, generated from existing load/result contracts. |
| IV/V | Private token file, stripped Hub credentials, exact admin manifest and one executor. |
| VI | Node launch span and outcome metric cover preflight/spawn; load success waits for readiness. |
| VII | Fake-process and local-manifest tests precede code; live readiness remains open. |

## Approach

Add a dedicated node image worker port setting outside the language engine range. Implement `ImageProcessSupervisor` with one lock covering preflight, budget, private file creation, spawn and atomic record. Use a narrow environment and a versioned Python executable, no shell. Return `starting` with persisted process identity; the next slice verifies readiness and re-adopts it after restart.
