# Implementation Plan: Vision Readiness Generation

**Branch**: `feat/014bc-vision-readiness` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Use the existing readiness probe, setting the `model` field only for an owned `mlx_vlm` engine. Resolve the path through the verified node store. Keep the health precheck and successful one-token response requirement. Add a mocked lifecycle contract without starting Metal or an engine process.

## Constitution Check

| Principle | Check |
| --- | --- |
| I/II | The bare engine remains Studio-owned; the test mocks its HTTP response. |
| III | No wire contract change at the control plane; the bare engine receives its required field. |
| IV/V | The probe uses the verified local store path from owned state, not caller input. |
| VI | Existing load span/metric and structured node logs report the transition. |
| VII | Focused mocked lifecycle and node regression tests; tiny-model integration stays open. |

No new dependency or architecture deviation.
