# Implementation Plan: Image registry kinds

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I | Only the generation base names the direct mflux engine. |
| II/II-a | Shared contracts only; no core process or container change. |
| III | Registry and engine wire types stay strict coire-core models. |
| IV | Unknown kinds/backends are refused. |
| V | Auxiliary assets cannot route as chat or launch as engines. |
| VI | No executable service path is added. |
| VII | Legacy and new-shape contract tests precede implementation. |

## Approach

Add compatible defaults to registry models, a distinct non-routable auxiliary backend, and kind/backend validation. Engine-start validation refuses auxiliary starts. Actual registry persistence, acquisition, node commands and routing exclusion are separate parent tasks and remain disabled.

`@eslint/js==9.39.5` (MIT) is declared directly as a web development dependency because the existing ESLint config imports it; pnpm's strict layout otherwise leaves the required local lint gate unable to start. Both web lockfiles are updated. No runtime dependency is added.
