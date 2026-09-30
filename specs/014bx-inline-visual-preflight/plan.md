# Implementation Plan: Gated Inline Visual Preflight

**Branch**: `feat/014bx-inline-visual-preflight` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Carry the registry backend and measured visual capability through gateway resolution. Decode only the bounded data URI and inspect its PNG signature/IHDR dimensions with the standard library; pass the original compatible content parts to the bare Studio VLM. Keep default-off deployment configuration and document the incomplete normalization gate. Add route/context contracts and run source/type/OpenAPI/web and Python gates. Do not mark parent T057 complete until file-worker normalization, digest-scoped asset reuse and full visual context are implemented.

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II | Gateway only proxies to the registry-selected bare Studio engine; core loads no weights. |
| III | Existing strict core OpenAI content and measured visual capability contracts remain authoritative. |
| IV/V | Authenticated entitlement resolution precedes image admission; no caller model path or remote image URL reaches an engine. |
| VI | Existing gateway usage/spans capture admission and failure; parent visual telemetry remains open. |
| VII | Contract and context tests cover accept/refuse paths; experimental path defaults off. |

No dependency or migration.
