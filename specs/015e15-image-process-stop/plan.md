# Implementation Plan: Fenced image worker process stop

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II | Only coire-node signals its resident Studio child; core hosts none. |
| II-a | No new container/service. |
| III | Uses strict `ImageWorkerUnloadRequest` and `ImageWorkerLoadResult`. |
| IV/V | PID/create-time/argv fence before signal; unknown state retains the hold. |
| VI | Node cleanup span and outcome metric; no prompt or path labels. |
| VII | Fake-process TERM/KILL and uncertain-state tests precede code. |

## Approach

Refine process inspection into same/gone/unknown states. On a matching unload request, inspect before signalling the child process group. TERM, poll with a monotonic deadline and escalate to KILL within five seconds. Confirm death before removing the durable record and private launch directory; do not touch generated scratch. Preserve reservation and record on uncertainty or cleanup failure.
