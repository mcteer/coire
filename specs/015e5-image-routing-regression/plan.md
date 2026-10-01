# Implementation Plan: Image routing isolation acceptance

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I, II | No engine, core inference, or user harness change. |
| III | Existing registry kind/backend contract and snapshot wire shape. |
| IV, V | Image assets remain invisible to chat/vision/MCP/failover resolution. |
| VI | Existing gateway and snapshot telemetry retained. |
| VII | Regression tests and full gates before parent completion. |

## Approach

Exercise the existing kind/backend predicates and the signed snapshot service with image and auxiliary rows. If any path leaks a row, fix the predicate before marking T028 complete.
