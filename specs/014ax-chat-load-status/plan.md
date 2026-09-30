# Implementation Plan: Observed Chat Load Status

**Branch**: `feat/014ax-chat-load-status` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

During the existing cold-load wait loop, read the latest active model-instance placement state. Emit a status event only when the mapped queued/loading phase changes, using the existing transactional event writer. Do not derive queue ranks or percentages. Keep the generic initial loading event and the measured historical warm-up duration behavior. Test each placement mapping and a cold turn's loading-to-queued-to-running event sequence.

## Constitution Check

| Principle | Check |
| --- | --- |
| III | Reuse typed core `turn.status` events. |
| IV | Existing owner stream, Stop and entitlement checks remain in the wait loop. |
| VI | Persisted events and existing turn telemetry cover each transition. |
| VII | Focused stream tests and full API/image gates. |

No constitution exception or dependency.
