# Implementation Plan: Chat Parser, File and Vision Observability

**Branch**: `feat/014bj-chat-observability` | **Parent**: [014 plan](../014-chat-web-ui/plan.md)

Record one parser-failure counter when a native turn ends without a valid complete engine stream. Add a fixed backend label to existing node load/resident measurements. Use the worker's existing outcome counter. Provision panels and baseline alert rules, test wiring, and run API/node and observability gates.

## Constitution Check

| Principle | Check |
| --- | --- |
| I/II | Node telemetry observes the existing bare engine only. |
| III | No wire shape changes. |
| IV/V | Metric labels contain fixed reason/outcome/backend values, without private content or paths. |
| VI | Dashboard and alerts cover parser, worker and VLM load paths. |
| VII | Metric path and configuration checks gate the change. |

No dependency, migration or architecture deviation.
