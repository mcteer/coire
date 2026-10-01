# Implementation Plan: Reconcile Studio image job status

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II | Only Studio node queries its private native worker. |
| III | Existing `ImageJobBinding`, `ImageWorkerStatus`, `NodeImageJob` are typed coire-core wires. |
| IV/V | Node bearer, exact fence, exact live process and private loopback credential. |
| VI | Fixed-label status span/metric and content-free error. |
| VII | Contract tests use a fake worker; real Studio gate remains open. |

## Approach

Add a status method to the node dispatcher and a GET route. Read the journal first. The worker `/status` command is observation-only, and its response can only advance the persisted finite-state machine. Do not transform a timeout into a new run.
