# Implementation Plan: Chat Code Controls

**Branch**: `feat/014bp-code-web-controls` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Expose the coding call ID on a strict Chat turn projection. Filter picker results by the coding profile and validated variant for the chosen action. Add generated-type web clients and Code controls to the existing Chat page. Render bounded activity and typed results, checking owner artifact metadata before offering a download. Keep source revision inherited from a prior Research or Plan result. Cover browser submission, controls, result, picker and origin behavior.

## Constitution Check

| Principle | Check |
| --- | --- |
| I/II | Controls use the existing Studio run path; core still runs no model. |
| III | Chat turn ID and event/result types come from core and regenerated OpenAPI/TypeScript. |
| IV/V | Picker enforces coding eligibility and Apply verification; workspace mutations enforce exact origin; downloads retain owner and expiry checks. |
| VI | Existing coding run activity and artifact telemetry remains on the shared paths. |
| VII | Contract, component, page and existing regression suites gate this slice. |

No new dependency, migration or architecture deviation.
