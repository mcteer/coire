# Implementation Plan: Cancel Abandoned Cold Loads

**Branch**: `feat/014bi-cold-stream-cancel` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Add one small shared task-lifetime context in gateway execution. Use it around cold-load wait loops in OpenAI, Anthropic and native Chat, so closing the generator runs cancellation and drains the task. Keep the existing resolution, proxy and usage paths. Test generator close after a keepalive and run the API regression gates.

## Constitution Check

| Principle | Check |
| --- | --- |
| I/II | The existing bare engine and node-owned load workflow remain unchanged. |
| III | No wire shape changes; existing `/v1` adapters stay compatible. |
| IV/V | Existing scope, model resolution and entitlement gates remain in place. |
| VI | Existing load and stream telemetry records outcomes. |
| VII | Cold stream cancellation and regression tests gate the change. |

No dependency, migration or architecture deviation.
