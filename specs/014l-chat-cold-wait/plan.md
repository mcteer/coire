# Implementation Plan: Cold Chat Wait

**Branch**: `feat/014l-chat-cold-wait` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Read the most recent non-null `EngineProcessRow.load_seconds` in a short session before the cold load. Persist that nullable estimate with `turn.status=loading` before emitting it. Keep the existing bounded gateway wait and comment keepalives. Distinguish a load failure from later generation failure using a safe terminal suggestion. The composer renders the estimate from the actual status event, and the picker already renders known or unknown measured values. No queue rank is shown because the placement service has no rank contract.

## Constitution Check

| Principle | Check |
| --- | --- |
| I, II, II-a | Existing bare Studio engine loader and gateway only. |
| III | Existing typed `ChatTurnStatus` and `ChatTurnTerminal`. |
| IV | Owner-scoped stream; no internal load exception exposed. |
| V | Registry model ID and measured engine history only. |
| VI | Existing send terminal metrics, span and alert. |
| VII | Backend and browser regression tests. |

No constitution exception or dependency.
