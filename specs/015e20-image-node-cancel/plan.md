# Implementation Plan: Fenced Studio image job cancellation

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II | Only the Studio node controls the native worker process. |
| III | Existing coire-core cancel, job and unload models define wire shapes. |
| IV/V | Node bearer, exact attempt/fence and exact process identity before TERM/KILL. |
| VI | Fixed-label cancel span/metric, content-free failure and retained uncertain hold. |
| VII | Contract tests cover cooperative, escalated and uncertain cancellation. |

## Approach

Add a cancel method to the node dispatcher and a DELETE route. Write intent first, use the private worker cancel command once, poll briefly, then call the existing supervisor stop which verifies PID/create time and retains reservation on uncertainty. Repeat calls return the journaled terminal state or retry only observation/escalation, never generation.
