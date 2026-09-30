# Implementation Plan: Honest Gateway Visual Refusal

**Branch**: `feat/014az-gateway-visual-guard` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Teach the existing context estimator to sum typed text-part lengths and raise an explicit unavailable-visual error for image parts. Map that refusal to a 400 before run spend or engine dispatch. Validate Anthropic blocks as text-only before resolution, and guard the adapter itself against direct misuse. Keep this refusal until the later worker-backed visual gateway path can account for image bytes, pixels and tokens.

## Constitution Check

| Principle | Check |
| --- | --- |
| III | Reuse core OpenAI/Anthropic contracts and return a clear compatible error. |
| IV | Refuse unsupported inputs before spend, model load or engine transport. |
| V | No caller image path reaches a bare engine. |
| VI | Existing gateway refusal accounting records the reason without content. |
| VII | Unit and route contracts plus full API/image gates. |

No constitution exception or dependency.
