# Implementation Plan: Fenced Studio image job dispatch

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II | Only coire-node calls the resident bare Studio worker. |
| III | Existing coire-core `NodeImageStartRequest`, `NodeImageJob` and `ImageWorkerRunRequest` define every wire shape. |
| IV/V | Existing node bearer, exact fenced binding, owner-only loopback secret and verified ready instance. |
| VI | Fixed-label node journal and generation stage spans/metrics; content-free errors. |
| VII | Contract and replay tests before code; real Studio acceptance remains open. |

## Approach

Add a narrow node job service and route, using the prior journal. The service holds a per-process lock from first journal lookup through one dispatch. It persists `reserving` before a private worker command and does not retry a timed-out call. Status polling, transfer and cancellation follow in separate slices.
