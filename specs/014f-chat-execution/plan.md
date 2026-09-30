# Implementation Plan: Shared Gateway Stream Execution

**Branch**: `feat/014f-chat-execution` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Move the compatible route's stream tracking, cancellation-resistant response, and public model rewrite into `coire_api.gateway.execution`. Re-export the current helpers from `routes.v1` to preserve callers, and keep route-local resolution/admission for now. The shared module will be the gateway primitive for native Chat. Prove the existing contracts before and after extraction, then add direct regression tests for the module. Keep the proxy's lease and engine slot unchanged.

## Constitution Check

| Principle | Check |
| --- | --- |
| I, II, II-a | Only existing gateway code moves; no engine or deployment change. |
| III | Existing compatible wire shapes remain unchanged. |
| IV | Credential recheck and cancellation accounting stay in the shared path. |
| V | Registry model ID rewrite and existing resolution are preserved. |
| VI | Existing first-token metrics and structured logs move with the path. |
| VII | Direct regression and compatible contract tests gate the extraction. |

No constitution exception, dependency or migration.
