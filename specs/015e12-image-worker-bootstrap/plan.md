# Implementation Plan: Offline Studio image worker bootstrap

**Spec**: [spec.md](spec.md)

## Constitution Check

| Principle | Compliance |
| --- | --- |
| I/II | Worker process runs only under coire-node on a Studio and calls direct mflux. |
| II-a | Same native node installation; no new container. |
| III | Strict `ImageWorkerProcessConfig` in coire-core before the process boundary. |
| IV/V | 0600 node-owned files, per-worker secret, offline flags before mflux import, exact Store preflight. |
| VI | Load telemetry is applied by the node supervisor in the later launcher slice. |
| VII | Config and fake-bootstrap tests precede code; live Studio test remains open. |

## Approach

Add a strict process config schema. Implement a tiny `python -m coire_node.image_runtime.bootstrap <config>` module using only stdlib and coire-core at import time. It validates file ownership and mode, strips Hub credentials and sets offline flags, then lazily imports Store/pipeline/control modules. The future manager creates the config and token, spawns the process with explicit argv and records PID/port/reservation.
